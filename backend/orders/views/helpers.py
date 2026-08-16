# orders/views/helpers.py
import os
import logging
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.validators import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.conf import settings

# 🛡️ Safely import python-magic so the app doesn't crash if the C-library is missing
try:
    import magic
except ImportError:
    magic = None

# 🛡️ Safely import resend for email sending
try:
    import resend
except ImportError:
    resend = None

from orders.models import Order

logger = logging.getLogger(__name__)
User = get_user_model()

# Security: Enhanced file validation
ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.txt', '.png', '.jpg', '.jpeg', '.pptx'}
ALLOWED_MIME_TYPES = {
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'text/plain',
    'image/png',
    'image/jpeg',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation'
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024


def _user_role(user):
    """
    Safely retrieves the user's role.
    Handles missing attributes, fallbacks to profile, and normalizes to lowercase string.
    """
    role = getattr(user, 'role', None)
    if role is None:
        profile = getattr(user, 'profile', None)
        if profile:
            role = getattr(profile, 'role', None)
    return str(role).lower().strip() if role else None


def _is_staff_role(user):
    """Checks if the user has any privileged/staff role."""
    role = _user_role(user)
    return role in ('admin', 'agent', 'super_admin', 'manager', 'staff')


def validate_upload_file(file):
    """Enhanced file validation with extension, size, and MIME type checking."""
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ', '.join(sorted(ALLOWED_EXTENSIONS))
        return f'Invalid file type. Allowed: {allowed}'
    
    if file.size > MAX_UPLOAD_SIZE:
        return 'File size exceeds 10MB limit.'
    
    if magic is None:
        return None
        
    try:
        file_content = file.read(1024)
        mime = magic.from_buffer(file_content, mime=True)
        file.seek(0)
        
        if mime not in ALLOWED_MIME_TYPES:
            logger.warning(f"Blocked upload: extension {ext}, MIME type {mime}")
            return f'File type not allowed. Detected type: {mime}'
    except Exception as e:
        logger.error(f"Error checking MIME type: {e}", exc_info=True)
            
    return None


def _can_view_order(user, order):
    """Determines if a user has permission to view a specific order."""
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    if _is_staff_role(user):
        return True
    return order.client == user


def _build_order_queryset(request):
    """Build filtered order queryset with proper validation."""
    qs = Order.objects.select_related('client', 'station', 'delivery_zone').order_by('-created_at')
    
    status = request.GET.get('status', '').strip()
    if status and status in dict(Order.STATUS_CHOICES).keys():
        qs = qs.filter(status=status)
        
    station_id = request.GET.get('station', '').strip()
    if station_id and station_id.isdigit():
        qs = qs.filter(station_id=int(station_id))
        
    order_type = request.GET.get('order_type', '').strip()
    if order_type and order_type in dict(Order.ORDER_TYPE_CHOICES).keys():
        qs = qs.filter(order_type=order_type)
        
    date_filter = request.GET.get('date', '').strip()
    now = timezone.now()
    if date_filter == 'today':
        qs = qs.filter(created_at__date=now.date())
    elif date_filter == 'week':
        qs = qs.filter(created_at__gte=now - timedelta(days=7))
    elif date_filter == 'month':
        qs = qs.filter(created_at__gte=now - timedelta(days=30))
        
    search = request.GET.get('search', '').strip()[:100]
    if search:
        if search.isdigit():
            qs = qs.filter(Q(id=int(search)) | Q(client__email__icontains=search))
        else:
            qs = qs.filter(
                Q(client__email__icontains=search) |
                Q(client__username__icontains=search) |
                Q(file_name__icontains=search)
            )
    return qs


def _order_summary_counts():
    """Get order summary counts efficiently."""
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        'total': Order.objects.count(),
        'pending': Order.objects.filter(status='pending').count(),
        'paid': Order.objects.filter(status='paid').count(),
        'printing': Order.objects.filter(status='printing').count(),
        'in_transit': Order.objects.filter(status='in_transit').count(),
        'ready': Order.objects.filter(status='ready').count(),
        'collected_today': Order.objects.filter(status='collected', collected_at__gte=today_start).count(),
        'cancelled': Order.objects.filter(status='cancelled').count(),
        'passport_orders': Order.objects.filter(order_type='passport').count(),
        'scanned_orders': Order.objects.filter(order_type='scanned').count(),
    }


def _get_tracked_orders(order_id=None, email=None):
    """Fetch orders for tracking page."""
    qs = Order.objects.select_related('station', 'client', 'delivery_zone')
    if order_id:
        if str(order_id).isdigit():
            return qs.filter(id=int(order_id))
        return Order.objects.none()
    if email:
        try:
            from django.core.validators import validate_email
            validate_email(email)
            return qs.filter(client__email__iexact=email).order_by('-created_at')
        except ValidationError:
            return Order.objects.none()
    return Order.objects.none()

_get_tracked_order = _get_tracked_orders


def is_agent_or_admin(user):
    """Check if user is an agent or admin (used for email sending and permissions)."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    role = _user_role(user)
    return role in ('admin', 'agent', 'super_admin', 'manager', 'staff')


# ============================================================
# 📧 EMAIL SERVICE - RESEND (Works on Render)
# ============================================================

def _send_email(to_email: str, subject: str, html_content: str, text_content: str = '') -> bool:
    """
    Send email using Resend API (bypasses Render's Port 25 block).
    Falls back to Django's send_mail if Resend is not configured.
    """
    # Try Resend first
    if resend is not None and hasattr(settings, 'RESEND_API_KEY') and settings.RESEND_API_KEY:
        try:
            resend.api_key = settings.RESEND_API_KEY
            
            params = {
                "from": getattr(settings, 'DEFAULT_FROM_EMAIL', 'PrintHub <onboarding@resend.dev>'),
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            }
            
            # Add text fallback if provided
            if text_content:
                params["text"] = text_content
                
            response = resend.Emails.send(params)
            logger.info(f"✅ Email sent via Resend to {to_email}: {subject}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Resend API error: {e}")
            # Fall through to Django send_mail
    
    # Fallback: Try Django's send_mail (works locally with console backend)
    try:
        from django.core.mail import send_mail
        send_mail(
            subject=subject,
            message=text_content or f"Please view in HTML-capable email client.",
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=[to_email],
            fail_silently=False,  # Changed to False so we see errors locally
            html_message=html_content if hasattr(settings, 'DEBUG') and settings.DEBUG else None,
        )
        logger.info(f"✅ Email sent via Django to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"❌ Django send_mail error: {e}")
        return False


def send_order_confirmation_email(order) -> bool:
    """Send order confirmation email with HTML template."""
    if not order.client.email:
        logger.warning(f"Order #{order.id} confirmation skipped: User has no email.")
        return False

    try:
        subject = f'Order #{order.id} Confirmed - PrintHub'
        
        # Build order type info
        if order.order_type == 'passport':
            order_type_html = f"""
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order Type</strong></td><td style="padding:8px;border:1px solid #ddd;">Passport Photo</td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Photo Size</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.get_paper_size_display()}</td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Copies</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.copies}</td></tr>
            """
        elif order.order_type == 'scanned':
            order_type_html = f"""
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order Type</strong></td><td style="padding:8px;border:1px solid #ddd;">Scanned Document</td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Paper Size</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.get_paper_size_display()}</td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Copies</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.copies}</td></tr>
            """
        else:
            order_type_html = f"""
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Paper Size</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.get_paper_size_display()}</td></tr>
            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Copies</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.copies}</td></tr>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9fafb; padding: 30px; border-radius: 0 0 10px 10px; }}
                .order-box {{ background: white; border: 2px solid #e5e7eb; border-radius: 8px; padding: 20px; margin: 20px 0; }}
                .status-badge {{ display: inline-block; background: #fef3c7; color: #92400e; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
                .btn {{ display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-top: 20px; }}
                .footer {{ text-align: center; padding: 20px; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin:0;">🖨️ PrintHub</h1>
                    <p style="margin:10px 0 0 0;">Order Confirmation</p>
                </div>
                <div class="content">
                    <h2>Hi {order.client.username},</h2>
                    <p>Your print order has been received! <span class="status-badge">PENDING</span></p>
                    
                    <div class="order-box">
                        <h3 style="margin-top:0;color:#667eea;">Order Details</h3>
                        <table style="width:100%;border-collapse:collapse;">
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order ID</strong></td><td style="padding:8px;border:1px solid #ddd;">#{order.id}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>File</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.file_name}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Pages</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.page_count}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Color</strong></td><td style="padding:8px;border:1px solid #ddd;">{'Yes' if order.is_color else 'No'}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Double-sided</strong></td><td style="padding:8px;border:1px solid #ddd;">{'Yes' if order.is_double_sided else 'No'}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Binding</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.get_binding_display()}</td></tr>
                            {order_type_html}
                            <tr style="background:#f0fdf4;"><td style="padding:12px;border:1px solid #ddd;"><strong>Total</strong></td><td style="padding:12px;border:1px solid #ddd;"><strong style="color:#16a34a;font-size:18px;">{order.total_price:,.0f} UGX</strong></td></tr>
                        </table>
                    </div>
                    
                    <p style="text-align:center;">
                        <a href="{getattr(settings, 'SITE_URL', '')}/track/?order_id={order.id}" class="btn">📍 Track Your Order</a>
                    </p>
                    
                    <p style="color:#6b7280;font-size:14px;">We'll notify you via WhatsApp when your order is ready for pickup.</p>
                </div>
                <div class="footer">
                    <p>Thank you for choosing PrintHub!<br>Kabale University Printing Service</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Plain text fallback
        text_content = f"""
Dear {order.client.username},

Your print order has been received!

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Pages: {order.page_count}
- Color: {'Yes' if order.is_color else 'No'}
- Total: {order.total_price:,.0f} UGX

Track your order at: {getattr(settings, 'SITE_URL', '')}/track/?order_id={order.id}

Thank you for choosing PrintHub!
"""
        
        return _send_email(order.client.email, subject, html_content, text_content)
        
    except Exception as e:
        logger.error(f"Failed to send confirmation email for order #{order.id}: {e}", exc_info=True)
        return False


def send_cancellation_email(order, reason='') -> bool:
    """Send order cancellation email with HTML template."""
    if not order.client.email:
        logger.warning(f"Order #{order.id} cancellation email skipped: User has no email.")
        return False

    try:
        subject = f'Order #{order.id} Cancelled - PrintHub'
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9fafb; padding: 30px; border-radius: 0 0 10px 10px; }}
                .order-box {{ background: white; border: 2px solid #fecaca; border-radius: 8px; padding: 20px; margin: 20px 0; }}
                .status-badge {{ display: inline-block; background: #fee2e2; color: #dc2626; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }}
                .btn {{ display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-top: 20px; }}
                .footer {{ text-align: center; padding: 20px; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin:0;">🖨️ PrintHub</h1>
                    <p style="margin:10px 0 0 0;">Order Cancelled</p>
                </div>
                <div class="content">
                    <h2>Hi {order.client.username},</h2>
                    <p>Your order has been cancelled. <span class="status-badge">CANCELLED</span></p>
                    
                    <div class="order-box">
                        <h3 style="margin-top:0;color:#dc2626;">Cancellation Details</h3>
                        <table style="width:100%;border-collapse:collapse;">
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Order ID</strong></td><td style="padding:8px;border:1px solid #ddd;">#{order.id}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>File</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.file_name}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Date</strong></td><td style="padding:8px;border:1px solid #ddd;">{order.created_at.strftime('%Y-%m-%d %H:%M')}</td></tr>
                            <tr><td style="padding:8px;border:1px solid #ddd;"><strong>Reason</strong></td><td style="padding:8px;border:1px solid #ddd;">{reason or 'Not specified'}</td></tr>
                        </table>
                    </div>
                    
                    <p style="text-align:center;">
                        <a href="{getattr(settings, 'SITE_URL', '')}/upload/" class="btn">📄 Place a New Order</a>
                    </p>
                </div>
                <div class="footer">
                    <p>PrintHub Team<br>Kabale University Printing Service</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_content = f"""
Dear {order.client.username},

Your order has been cancelled.

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Reason: {reason or 'Not specified'}

Place a new order at: {getattr(settings, 'SITE_URL', '')}/upload/

Thank you,
PrintHub Team
"""
        
        return _send_email(order.client.email, subject, html_content, text_content)
        
    except Exception as e:
        logger.error(f"Failed to send cancellation email for order #{order.id}: {e}", exc_info=True)
        return False
