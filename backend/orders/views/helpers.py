# orders/views/helpers.py
import os
import logging
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.validators import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.core.mail import send_mail

# 🛡️ Safely import python-magic so the app doesn't crash if the C-library is missing
try:
    import magic
except ImportError:
    magic = None

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
        # Fallback if role ever lives on a profile object
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
    # 1. Check extension
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ', '.join(sorted(ALLOWED_EXTENSIONS))
        return f'Invalid file type. Allowed: {allowed}'
    
    # 2. Check size
    if file.size > MAX_UPLOAD_SIZE:
        return 'File size exceeds 10MB limit.'
    
    # 3. Check MIME type
    if magic is None:
        return None  # Skip MIME check if magic library isn't installed
        
    try:
        # Read first 1024 bytes to detect MIME
        file_content = file.read(1024)
        mime = magic.from_buffer(file_content, mime=True)
        file.seek(0)  # Reset pointer so Django can read the whole file later
        
        if mime not in ALLOWED_MIME_TYPES:
            logger.warning(f"Blocked upload: extension {ext}, MIME type {mime}")
            return f'File type not allowed. Detected type: {mime}'
    except Exception as e:
        logger.error(f"Error checking MIME type: {e}", exc_info=True)
        # Fail open if magic throws an unexpected OS-level error
            
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

# 🛡️ Alias for client_views.py compatibility (it imports the singular name)
_get_tracked_order = _get_tracked_orders


def is_agent_or_admin(user):
    """Check if user is an agent or admin (used for email sending and permissions)."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return True
    role = _user_role(user)
    return role in ('admin', 'agent', 'super_admin', 'manager', 'staff')


def send_order_confirmation_email(order):
    """Send order confirmation email safely."""
    if not order.client.email:
        logger.warning(f"Order #{order.id} confirmation skipped: User has no email.")
        return

    try:
        subject = f'Order #{order.id} Confirmed - PrintHub'
        order_type_info = ""
        if order.order_type == 'passport':
            order_type_info = f"\nOrder Type: Passport Photo\nPhoto Size: {order.get_paper_size_display()}\nCopies: {order.copies}\n"
        elif order.order_type == 'scanned':
            order_type_info = f"\nOrder Type: Scanned Document\nPaper Size: {order.get_paper_size_display()}\nCopies: {order.copies}\n"
        else:
            order_type_info = f"\nPaper Size: {order.get_paper_size_display()}\nCopies: {order.copies}\n"
            
        message = f"""Dear {order.client.username},

Your print order has been received!

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Pages: {order.page_count}
- Color: {'Yes' if order.is_color else 'No'}
- Double-sided: {'Yes' if order.is_double_sided else 'No'}
- Binding: {order.get_binding_display()}{order_type_info}
- Total: {order.total_price:,.0f} UGX

Track your order at: {getattr(settings, 'SITE_URL', '')}/track/?order_id={order.id}

Thank you for choosing PrintHub!
"""
        send_mail(
            subject, 
            message, 
            settings.DEFAULT_FROM_EMAIL, 
            [order.client.email], 
            fail_silently=True
        )
    except Exception as e:
        logger.error(f"Failed to send confirmation email for order #{order.id}: {e}", exc_info=True)


def send_cancellation_email(order, reason=''):
    """Send order cancellation email safely."""
    if not order.client.email:
        logger.warning(f"Order #{order.id} cancellation email skipped: User has no email.")
        return

    try:
        subject = f'Order #{order.id} Cancelled - PrintHub'
        message = f"""Dear {order.client.username},

Your order has been cancelled as requested.

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}
- Status: Cancelled

Reason for cancellation: {reason or 'Not specified'}

Place a new order at: {getattr(settings, 'SITE_URL', '')}/upload/

Thank you,
PrintHub Team
"""
        send_mail(
            subject, 
            message, 
            settings.DEFAULT_FROM_EMAIL, 
            [order.client.email], 
            fail_silently=True
        )
    except Exception as e:
        logger.error(f"Failed to send cancellation email for order #{order.id}: {e}", exc_info=True)
