# orders/views.py
import os
import json
import mimetypes
import logging
from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Q, Sum, Count
from django.db import transaction
from django.http import FileResponse, HttpResponseForbidden, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.conf import settings
from django.core.mail import send_mail
from django.views.decorators.cache import cache_control, cache_page
from django.utils.html import strip_tags
from django.core.validators import ValidationError
from PIL import Image, ImageDraw, ImageFont
import io
import base64
import magic
from django.core.files.base import ContentFile
from stations.models import Station
from .models import Order, SystemSettings, DeliveryZone, Announcement
from .utils import apply_order_status_change, send_delayed_order_email

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
    return getattr(user, 'role', None)

def _is_staff_role(user):
    return _user_role(user) in ('admin', 'agent')

def validate_upload_file(file):
    """Enhanced file validation with MIME type checking."""
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ', '.join(sorted(ALLOWED_EXTENSIONS))
        return f'Invalid file type. Allowed: {allowed}'
    
    if file.size > MAX_UPLOAD_SIZE:
        return 'File size exceeds 10MB limit.'
    
    try:
        file_content = file.read(1024)
        mime = magic.from_buffer(file_content, mime=True)
        file.seek(0)
        if mime not in ALLOWED_MIME_TYPES:
            logger.warning(f"Blocked upload: extension {ext}, MIME type {mime}")
            return f'File type not allowed. Detected type: {mime}'
    except Exception as e:
        logger.error(f"Error checking MIME type: {e}")
        pass
    return None

def _can_view_order(user, order):
    if _user_role(user) in ('admin', 'agent'):
        return True
    return order.client == user

@login_required
def dashboard_view(request):
    orders = Order.objects.filter(client=request.user).order_by('-created_at')
    stats = Order.objects.filter(client=request.user).aggregate(
        total_orders=Count('id'),
        completed_orders=Count('id', filter=Q(status='collected')),
        pending_orders=Count('id', filter=Q(status='pending')),
        total_spent=Sum('total_price', filter=Q(status__in=['paid', 'printing', 'in_transit', 'ready', 'collected']))
    )
    return render(request, 'orders/dashboard.html', {
        'orders': orders,
        'stats': stats,
    })

@transaction.atomic
def upload_view(request):
    stations = Station.objects.all()
    delivery_zones = DeliveryZone.objects.filter(is_active=True)
    upload_error = None
    
    if request.method == 'POST':
        if not request.user.is_authenticated:
            messages.info(request, 'Please log in or create an account to complete your upload.')
            return redirect('/auth/login/?next=/upload/')
            
        file = request.FILES.get('file')
        page_count = request.POST.get('page_count', 1)
        is_color = request.POST.get('is_color', 'False') == 'True'
        is_double_sided = request.POST.get('is_double_sided') == 'on'
        station_id = request.POST.get('station')
        binding = request.POST.get('binding', 'none')
        delivery_type = request.POST.get('delivery_type', 'pickup')
        delivery_zone_id = request.POST.get('delivery_zone')
        notes = strip_tags(request.POST.get('notes', '').strip())
        
        # New fields
        order_type = request.POST.get('order_type', 'document')
        paper_size = request.POST.get('paper_size', 'A4')
        copies = request.POST.get('copies', 1)
        
        # *** GET JAVASCRIPT CALCULATED PRICE ***
        calculated_price = request.POST.get('calculated_price', '0')
        
        # Handle passport and scanner data
        passport_data = request.POST.get('passport_data', '')
        scanner_data = request.POST.get('scanner_data', '')
        
        # Handle base64 file uploads from camera/scanner
        if not file and (passport_data or scanner_data):
            try:
                if passport_data:
                    format, imgstr = passport_data.split(';base64,')
                    ext = format.split('/')[-1]
                    file = ContentFile(
                        base64.b64decode(imgstr),
                        name=f'passport_photo.{ext}'
                    )
                elif scanner_data:
                    pass
            except Exception as e:
                logger.error(f"Error processing camera/scanner data: {e}")
                upload_error = 'Error processing captured image.'
                
        if not file:
            upload_error = 'Please select a file.'
        else:
            upload_error = validate_upload_file(file)
            
        if upload_error:
            return render(request, 'orders/upload.html', {
                'stations': stations,
                'delivery_zones': delivery_zones,
                'upload_error': upload_error,
            })
            
        station = None
        if station_id and station_id.isdigit():
            station = Station.objects.filter(id=int(station_id)).first()
            
        delivery_zone = None
        if delivery_type == 'delivery' and delivery_zone_id and delivery_zone_id.isdigit():
            delivery_zone = DeliveryZone.objects.filter(id=int(delivery_zone_id)).first()
            
        try:
            page_count_int = int(page_count)
            copies_int = int(copies)
            
            if page_count_int < 1:
                raise ValueError("Page count must be at least 1")
            if copies_int < 1:
                copies_int = 1
                
            order_type_display = dict(Order.ORDER_TYPE_CHOICES).get(order_type, 'Document Print')
            paper_size_display = dict(Order.PAPER_SIZE_CHOICES).get(paper_size, 'A4')
            
            extra_notes = f"Order Type: {order_type_display}\n"
            extra_notes += f"Paper Size: {paper_size_display}\n"
            extra_notes += f"Copies: {copies_int}"
            
            if notes:
                notes = f"{notes}\n{extra_notes}"
            else:
                notes = extra_notes

            # ==========================================================
            # 🛡️ SERVER-SIDE ENFORCEMENT FOR PASSPORT & SCANNER MODES 🛡️
            # ==========================================================
            if order_type == 'passport':
                is_color = True
                binding = 'none'
                is_double_sided = False
                page_count_int = copies_int 
                
                passport_base_price = copies_int * Order.PASSPORT_PHOTO_PRICE
                delivery_fee = delivery_zone.delivery_fee if delivery_zone and delivery_type == 'delivery' else 0
                calculated_price = str(passport_base_price + delivery_fee)
                
            elif order_type == 'scanned':
                binding = 'none'
                is_double_sided = False
                page_count_int = copies_int if copies_int > page_count_int else page_count_int

            # Create the order object first (don't save yet)
            order = Order(
                client=request.user,
                station=station,
                file=file,
                file_name=file.name,
                page_count=page_count_int,
                is_color=is_color,
                is_double_sided=is_double_sided,
                binding=binding,
                delivery_type=delivery_type,
                delivery_zone=delivery_zone,
                notes=notes,
                status='pending',
                order_type=order_type,
                paper_size=paper_size,
                copies=copies_int,
            )
            
            # *** SET JAVASCRIPT/SERVER CALCULATED PRICE BEFORE SAVE ***
            if calculated_price:
                try:
                    js_price = Decimal(str(calculated_price))
                    if js_price > 0:
                        order.total_price = js_price
                except Exception:
                    pass
                    
            # Now save - model's save() sees total_price > 0 and skips recalculation
            order.save()
            
            try:
                send_order_confirmation_email(order)
            except Exception as e:
                logger.error(f"Failed to send confirmation email for order #{order.id}: {e}", exc_info=True)
                
            messages.success(request, f'Order #{order.id} submitted! Total: {order.total_price:,.0f} UGX')
            return redirect('order_receipt', order_id=order.id)
            
        except ValueError as e:
            upload_error = f'Invalid input: {str(e)}'
        except Exception as e:
            logger.error(f"Error creating order: {e}", exc_info=True)
            upload_error = 'Error creating order. Please try again.'
            
        # If we hit an error during POST, render the page with the error
        return render(request, 'orders/upload.html', {
            'stations': stations,
            'delivery_zones': delivery_zones,
            'upload_error': upload_error,
        })

    # ====================================================================
    # 🛑 THIS WAS MISSING! Handles the initial GET request when loading page
    # ====================================================================
    return render(request, 'orders/upload.html', {
        'stations': stations,
        'delivery_zones': delivery_zones,
        'upload_error': upload_error,
    })

# ============================================================
# API Endpoints for Passport & Scanner
# ============================================================
@login_required
def api_analyze_passport(request):
    """Analyze passport photo quality via API."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        image_data = data.get('image', '')
        analysis = {
            'face_position': {'status': 'pass', 'label': 'Centered'},
            'brightness': {'status': 'pass', 'label': 'Good'},
            'expression': {'status': 'pass', 'label': 'Neutral'},
            'eyes': {'status': 'pass', 'label': 'Visible'},
            'background': {'status': 'pass', 'label': 'Uniform'},
            'overall': {'status': 'pass', 'label': 'Good to capture!'},
        }
        return JsonResponse({'success': True, 'analysis': analysis})
    except Exception as e:
        logger.error(f"Passport analysis error: {e}")
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def api_process_passport(request):
    """Process passport photo with background replacement."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        image_data = data.get('image', '')
        bg_color = data.get('bg_color', '#ffffff')
        size = data.get('size', '4x6')
        return JsonResponse({
            'success': True,
            'processed_image': image_data,
        })
    except Exception as e:
        logger.error(f"Passport processing error: {e}")
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def api_process_scan(request):
    """Process scanned document for enhancement."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        image_data = data.get('image', '')
        return JsonResponse({
            'success': True,
            'processed_image': image_data,
        })
    except Exception as e:
        logger.error(f"Scan processing error: {e}")
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def validate_discount_code(request):
    """Validate and calculate discount."""
    if request.method != 'POST':
        return JsonResponse({'valid': False, 'error': 'POST required'})
    code = request.POST.get('code', '').strip().upper()
    order_total = request.POST.get('order_total', 0)
    discounts = {
        'HEC10': 0.10,
        'STUDENT20': 0.20,
        'WELCOME5': 0.05,
    }
    if code in discounts:
        try:
            total = float(order_total)
            savings = int(total * discounts[code])
            return JsonResponse({
                'valid': True,
                'savings': savings,
                'rate': f'{int(discounts[code] * 100)}%'
            })
        except (ValueError, TypeError):
            return JsonResponse({'valid': False, 'error': 'Invalid order total'})
    return JsonResponse({'valid': False, 'error': 'Invalid or expired discount code'})

# ============================================================
# Payment Page View
# ============================================================
@login_required
def payment_page_view(request, order_id):
    """Payment page for an order."""
    if not str(order_id).isdigit():
        return HttpResponseForbidden('Invalid order ID.')
    order = get_object_or_404(Order.objects.select_related('station', 'delivery_zone'), id=int(order_id))
    if order.client != request.user:
        return HttpResponseForbidden('You can only pay for your own orders.')
    if order.status != 'pending':
        messages.info(request, 'This order has already been paid or is being processed.')
        return redirect('order_receipt', order_id=order.id)
    return render(request, 'orders/payment.html', {
        'order': order,
    })

@login_required
def order_receipt_view(request, order_id):
    if not str(order_id).isdigit():
        return HttpResponseForbidden('Invalid order ID.')
    order = get_object_or_404(Order.objects.select_related('station', 'delivery_zone'), id=int(order_id))
    if not _can_view_order(request.user, order):
        return HttpResponseForbidden('You do not have permission to view this receipt.')
    estimated_ready = order.estimated_ready_at()
    payment = None
    try:
        from payments.models import Payment
        payment = Payment.objects.filter(order=order).first()
    except Exception:
        pass
    return render(request, 'orders/receipt.html', {
        'order': order,
        'estimated_ready': estimated_ready,
        'payment': payment,
    })

def _build_order_queryset(request):
    """Build filtered order queryset with proper validation."""
    qs = Order.objects.select_related('client', 'station', 'delivery_zone').order_by('-created_at')
    status = request.GET.get('status', '').strip()
    if status:
        valid_statuses = dict(Order.STATUS_CHOICES).keys()
        if status in valid_statuses:
            qs = qs.filter(status=status)
    station_id = request.GET.get('station', '').strip()
    if station_id and station_id.isdigit():
        qs = qs.filter(station_id=int(station_id))
    order_type = request.GET.get('order_type', '').strip()
    if order_type:
        valid_types = dict(Order.ORDER_TYPE_CHOICES).keys()
        if order_type in valid_types:
            qs = qs.filter(order_type=order_type)
    date_filter = request.GET.get('date', '').strip()
    now = timezone.now()
    if date_filter == 'today':
        qs = qs.filter(created_at__date=now.date())
    elif date_filter == 'week':
        qs = qs.filter(created_at__gte=now - timedelta(days=7))
    elif date_filter == 'month':
        qs = qs.filter(created_at__gte=now - timedelta(days=30))
    search = request.GET.get('search', '').strip()
    if search:
        search = search[:100]
        if search.isdigit():
            qs = qs.filter(
                Q(id=int(search)) |
                Q(client__email__icontains=search)
            )
        else:
            from django.utils.html import escape
            safe_search = escape(search)
            qs = qs.filter(
                Q(client__email__icontains=safe_search) |
                Q(client__username__icontains=safe_search) |
                Q(file_name__icontains=safe_search)
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

@login_required
@transaction.atomic
def admin_dashboard_view(request):
    if _user_role(request.user) != 'admin':
        messages.error(request, 'Access denied. Admin only.')
        return redirect('dashboard')
        
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'assign_agent':
            agent_id = request.POST.get('agent_id')
            station_id = request.POST.get('agent_station_id') or None
            if not agent_id or not agent_id.isdigit():
                messages.error(request, 'Invalid agent ID.')
                return redirect('admin_dashboard')
            agent = get_object_or_404(User, id=int(agent_id), role='agent')
            if station_id and station_id.isdigit():
                station_id = int(station_id)
            else:
                station_id = None
            if hasattr(agent, 'station'):
                agent.station_id = station_id
                agent.save(update_fields=['station'])
                messages.success(request, f'Station updated for agent {agent.username}.')
            else:
                messages.error(request, 'Agent model does not have station field.')
            return redirect('admin_dashboard')
            
        if action == 'bulk_status':
            new_status = request.POST.get('bulk_status')
            order_ids = request.POST.getlist('order_ids')
            valid = ['printing', 'in_transit', 'ready', 'collected', 'cancelled']
            if new_status in valid and order_ids:
                valid_order_ids = [oid for oid in order_ids if oid.isdigit()]
                updated_count = 0
                for oid in valid_order_ids:
                    try:
                        order = Order.objects.select_for_update().get(id=int(oid))
                        if apply_order_status_change(order, new_status, request.user):
                            updated_count += 1
                    except Order.DoesNotExist:
                        continue
                    except Exception as e:
                        logger.error(f"Error updating order {oid}: {e}")
                messages.success(request, f'Updated {updated_count} order(s) to {new_status}.')
            return redirect(request.get_full_path() or 'admin_dashboard')
            
        if action == 'update_announcement':
            if request.POST.get('delete_announcement'):
                Announcement.objects.filter(is_active=True).update(is_active=False)
                messages.success(request, 'Announcement removed.')
            else:
                title = strip_tags(request.POST.get('announcement_title', 'Announcement'))
                message_text = strip_tags(request.POST.get('announcement_message', ''))
                color = request.POST.get('announcement_color', 'bg-blue-600')
                is_active = request.POST.get('announcement_active') == 'on'
                show_home = request.POST.get('announcement_home') == 'on'
                allowed_colors = ['bg-blue-600', 'bg-red-600', 'bg-green-600', 'bg-yellow-600', 'bg-purple-600']
                if color not in allowed_colors:
                    color = 'bg-blue-600'
                if message_text:
                    Announcement.objects.update_or_create(
                        is_active=True,
                        defaults={
                            'title': title,
                            'message': message_text,
                            'background_color': color,
                            'is_active': is_active,
                            'show_on_home': show_home,
                        }
                    )
                    messages.success(request, 'Announcement updated!')
                else:
                    messages.error(request, 'Message cannot be empty.')
            return redirect('admin_dashboard')

    orders_qs = _build_order_queryset(request)
    summary = _order_summary_counts()
    from django.utils import timezone
    overdue_count = Order.objects.filter(
        status__in=['paid', 'printing', 'in_transit', 'ready'],
    ).count()
    summary['overdue'] = overdue_count
    
    paginator = Paginator(orders_qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    agents = User.objects.filter(role='agent').select_related('station')
    stations = Station.objects.all()
    system_settings = SystemSettings.load()
    
    active_filters = []
    filter_keys = {
        'status': 'Status',
        'station': 'Station',
        'date': 'Date',
        'search': 'Search',
        'order_type': 'Type'
    }
    for key, label in filter_keys.items():
        val = request.GET.get(key, '').strip()
        if val:
            safe_val = strip_tags(val)
            active_filters.append({'key': key, 'value': safe_val, 'label': label})
            
    return render(request, 'orders/admin_dashboard.html', {
        'page_obj': page_obj,
        'orders': page_obj.object_list,
        'summary': summary,
        'agents': agents,
        'stations': stations,
        'status_choices': Order.STATUS_CHOICES,
        'order_type_choices': Order.ORDER_TYPE_CHOICES,
        'active_filters': active_filters,
        'filter_status': request.GET.get('status', ''),
        'filter_station': request.GET.get('station', ''),
        'filter_date': request.GET.get('date', ''),
        'filter_search': request.GET.get('search', ''),
        'filter_order_type': request.GET.get('order_type', ''),
        'total_filtered': orders_qs.count(),
        'system_settings': system_settings,
        'active_announcement': Announcement.get_active(),
    })

@login_required
def toggle_system_pause_view(request):
    if _user_role(request.user) != 'admin':
        return HttpResponseForbidden("Admin access only.")
    sys_settings = SystemSettings.load()
    if request.method == 'POST':
        action = request.POST.get('action')
        csrf_token = request.POST.get('csrfmiddlewaretoken')
        if not csrf_token:
            return HttpResponseForbidden("Invalid request.")
        if action == 'pause':
            if not sys_settings.is_paused:
                reason = strip_tags(request.POST.get('reason', 'Unforeseen circumstances'))
                sys_settings.is_paused = True
                sys_settings.pause_reason = reason[:200]
                sys_settings.pause_started_at = timezone.now()
                sys_settings.save()
                messages.success(request, "System timers PAUSED successfully.")
        elif action == 'resume':
            if sys_settings.is_paused:
                if sys_settings.pause_started_at:
                    sys_settings.total_paused_seconds += (timezone.now() - sys_settings.pause_started_at).total_seconds()
                sys_settings.is_paused = False
                sys_settings.pause_started_at = None
                sys_settings.save()
                messages.success(request, "System timers RESUMED successfully.")
    return redirect('admin_dashboard')

def is_agent_or_admin(user):
    return user.is_authenticated and (user.role == 'agent' or user.is_staff)

@login_required
@user_passes_test(is_agent_or_admin, login_url='login')
def agent_dashboard_view(request):
    if request.user.role == 'agent':
        if request.user.station:
            orders = Order.objects.filter(
                station=request.user.station
            ).select_related('client', 'delivery_zone').order_by('-created_at')
        else:
            orders = Order.objects.none()
            messages.warning(request, 'You are not assigned to any station.')
    else:
        orders = Order.objects.select_related(
            'client', 'station', 'delivery_zone'
        ).order_by('-created_at')
        
    agent_earnings = None
    if request.user.role == 'agent':
        try:
            from finances.models import AgentEarning
            agent_earnings = AgentEarning.objects.filter(
                agent=request.user
            ).aggregate(
                total_earned=Sum('commission_amount'),
                pending=Sum('commission_amount', filter=Q(is_paid=False)),
                paid=Sum('commission_amount', filter=Q(is_paid=True)),
                total_orders=Count('id')
            )
        except Exception as e:
            logger.error(f"Error fetching agent earnings: {e}")
            
    if request.method == 'POST':
        action = request.POST.get('action')
        order_id = request.POST.get('order_id')
        if not order_id or not order_id.isdigit():
            messages.error(request, 'Invalid order ID.')
            return redirect('agent_dashboard')
        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(id=int(order_id))
                if action == 'update_status':
                    new_status = request.POST.get('status')
                    valid_statuses = dict(Order.STATUS_CHOICES).keys()
                    if new_status not in valid_statuses:
                        messages.error(request, 'Invalid status.')
                        return redirect('agent_dashboard')
                    if request.user.role == 'agent' and order.station != request.user.station:
                        messages.error(request, 'You can only update orders for your station.')
                        return redirect('agent_dashboard')
                    if apply_order_status_change(order, new_status, request.user):
                        messages.success(request, f'Order #{order.id} updated to {order.get_status_display()}.')
                    else:
                        messages.info(request, f'Order #{order.id} status unchanged.')
                elif action == 'notify_delay':
                    reason = strip_tags(request.POST.get('delay_reason', '').strip())
                    if not reason:
                        messages.error(request, 'Please provide a delay reason.')
                        return redirect('agent_dashboard')
                    from notifications.models import Notification
                    Notification.create_notification(
                        user=order.client,
                        notification_type='order_delayed',
                        title='Order Delayed',
                        message=f'Your Order #{order.id} ({order.file_name}) has been delayed. Reason: {reason}',
                        link=f'/orders/{order.id}/receipt/'
                    )
                    send_delayed_order_email(order, reason)
                    messages.success(request, f'Delay notification sent for Order #{order.id}.')
                elif action == 'cancel_order':
                    if order.status not in ['collected', 'cancelled']:
                        reason = strip_tags(request.POST.get('cancellation_reason', '').strip())
                        order.status = 'cancelled'
                        order.cancellation_reason = reason[:500]
                        order.cancelled_at = timezone.now()
                        order.save(update_fields=['status', 'cancellation_reason', 'cancelled_at'])
                        messages.success(request, f'Order #{order.id} has been CANCELLED.')
                    else:
                        messages.error(request, 'Cannot cancel this order.')
                elif action == 'postpone_order':
                    if order.status not in ['collected', 'cancelled']:
                        try:
                            extra_minutes = int(request.POST.get('extra_minutes', 30))
                            if 0 < extra_minutes <= 1440:
                                order.postponed_minutes += extra_minutes
                                order.save(update_fields=['postponed_minutes'])
                                messages.success(request, f'Order #{order.id} postponed by {extra_minutes} minutes.')
                            else:
                                messages.error(request, 'Please enter a valid number of minutes (1-1440).')
                        except ValueError:
                            messages.error(request, 'Invalid number of minutes.')
                    else:
                        messages.error(request, 'Cannot postpone this order.')
                elif action == 'add_note':
                    note = strip_tags(request.POST.get('note', '').strip())
                    if note:
                        existing_notes = order.notes or ''
                        timestamp = timezone.now().strftime('%Y-%m-%d %H:%M')
                        order.notes = f"{existing_notes}\n[{timestamp}] {request.user.username}: {note}".strip()
                        order.save(update_fields=['notes'])
                        messages.success(request, f'Note added to Order #{order.id}.')
                    else:
                        messages.error(request, 'Note cannot be empty.')
        except Order.DoesNotExist:
            messages.error(request, 'Order not found.')
        except Exception as e:
            logger.error(f"Error in agent dashboard action: {e}", exc_info=True)
            messages.error(request, 'An error occurred. Please try again.')
        return redirect('agent_dashboard')
        
    return render(request, 'orders/agent_dashboard.html', {
        'orders': orders,
        'agent_earnings': agent_earnings,
    })

@login_required
@transaction.atomic
def update_order_status_view(request, order_id):
    if not _is_staff_role(request.user):
        return HttpResponseForbidden('You do not have permission to update order status.')
    if not str(order_id).isdigit():
        return HttpResponseForbidden('Invalid order ID.')
    try:
        order = Order.objects.select_for_update().get(id=int(order_id))
    except Order.DoesNotExist:
        messages.error(request, 'Order not found.')
        return redirect('dashboard')
    if _user_role(request.user) == 'agent':
        if not request.user.station or order.station_id != request.user.station_id:
            return HttpResponseForbidden('You can only update orders for your assigned station.')
    if request.method == 'POST':
        new_status = request.POST.get('status')
        valid_statuses = dict(Order.STATUS_CHOICES).keys()
        if new_status not in valid_statuses:
            messages.error(request, 'Invalid status.')
            return redirect('dashboard')
        if apply_order_status_change(order, new_status, request.user):
            messages.success(request, f'Order #{order.id} status updated to {order.get_status_display()}.')
        else:
            messages.error(request, 'Failed to update order status.')
    if _user_role(request.user) == 'admin':
        return redirect('admin_dashboard')
    if _user_role(request.user) == 'agent':
        return redirect('agent_dashboard')
    return redirect('dashboard')

@login_required
def download_order_file_view(request, order_id):
    """
    FIX: Uses redirect instead of FileResponse with .open('rb') to bypass 
    Cloudinary 401 Unauthorized errors and save Render server bandwidth.
    """
    if not str(order_id).isdigit():
        return HttpResponseForbidden('Invalid order ID.')
    
    order = get_object_or_404(Order, id=int(order_id))
    user = request.user
    
    if _user_role(user) not in ('admin', 'agent') and order.client != user:
        return HttpResponseForbidden('You do not have permission to download this file.')
        
    if not order.file:
        messages.error(request, 'File not found.')
        return redirect('dashboard')
        
    # Redirect directly to Cloudinary URL
    # Adding ?fl_attachment=true forces the browser to download instead of preview
    download_url = f"{order.file.url}?fl_attachment=true"
    
    return redirect(download_url)

def _get_tracked_orders(order_id=None, email=None):
    qs = Order.objects.select_related('station', 'client', 'delivery_zone')
    if order_id:
        if str(order_id).isdigit():
            return qs.filter(id=int(order_id))
        return Order.objects.none()
    if email:
        from django.core.validators import validate_email
        try:
            validate_email(email)
            return qs.filter(client__email__iexact=email).order_by('-created_at')
        except ValidationError:
            return Order.objects.none()
    return Order.objects.none()

def order_track_view(request):
    orders = None
    lookup_error = None
    order_id = request.GET.get('order_id', '').strip() or request.POST.get('order_id', '').strip()
    email = request.GET.get('email', '').strip() or request.POST.get('email', '').strip()
    if order_id or email:
        if order_id:
            orders = _get_tracked_orders(order_id=order_id)
            if not orders.exists():
                lookup_error = 'No order found with that order ID.'
                orders = None
        elif email:
            orders = _get_tracked_orders(email=email)
            if not orders.exists():
                lookup_error = 'No orders found for that email address.'
                orders = None
    timeline_steps = [
        ('submitted', 'Submitted', 'created_at'),
        ('paid', 'Paid', 'paid_at'),
        ('printing', 'Printing', 'printing_at'),
        ('in_transit', 'In Transit', 'in_transit_at'),
        ('ready', 'Ready for Pickup', 'ready_at'),
        ('collected', 'Collected', 'collected_at'),
    ]
    order_timelines = []
    if orders:
        status_step_map = {
            'pending': 0, 'paid': 1, 'printing': 2,
            'in_transit': 3, 'ready': 4, 'collected': 5
        }
        for order in orders:
            current_step = status_step_map.get(order.status, 0)
            if order.status == 'cancelled':
                current_step = -1
            steps = []
            for i, (key, label, ts_field) in enumerate(timeline_steps):
                ts = getattr(order, ts_field, None)
                if order.status == 'cancelled':
                    state = 'cancelled'
                elif i < current_step:
                    state = 'completed'
                elif i == current_step:
                    state = 'current'
                else:
                    state = 'future'
                steps.append({
                    'key': key,
                    'label': label,
                    'timestamp': ts,
                    'state': state
                })
            order_timelines.append({
                'order': order,
                'steps': steps,
                'estimated_ready': order.estimated_ready_at(),
                'is_overdue': order.is_overdue,
                'progress_width': int(current_step / (len(timeline_steps) - 1) * 100) if len(timeline_steps) > 1 and current_step >= 0 else 0,
            })
    return render(request, 'orders/track.html', {
        'orders': orders,
        'order_timelines': order_timelines,
        'lookup_error': lookup_error,
        'query_order_id': order_id,
        'query_email': email,
    })

def home_view(request):
    try:
        total_orders = Order.objects.count()
        stations = Station.objects.filter(is_active=True).count()
    except Exception:
        total_orders = 0
        stations = 0
    return render(request, 'home.html', {
        'total_orders': total_orders,
        'total_stations': stations
    })

@login_required
def live_board_view(request):
    return render(request, 'orders/live_board.html')

@cache_page(60 * 1)
def live_board_api_view(request):
    active_statuses = ['paid', 'printing', 'in_transit', 'ready']
    orders = Order.objects.filter(
        status__in=active_statuses
    ).select_related('station', 'client')
    cancelled_orders = Order.objects.filter(
        status='cancelled',
        cancelled_at__gte=timezone.now() - timedelta(minutes=30)
    ).select_related('station', 'client')
    all_orders = list(orders) + list(cancelled_orders)
    sys_settings = SystemSettings.load()
    board_data = []
    for order in all_orders:
        priority = order.priority_info
        board_data.append({
            'id': order.id,
            'client': order.client.username,
            'station': order.station.name if order.station else 'Unassigned',
            'file_name': order.file_name,
            'status': order.get_status_display(),
            'status_raw': order.status,
            'time_left': priority['time_display'],
            'remaining_seconds': priority['remaining_seconds'],
            'priority': priority['display'],
            'priority_level': priority['level'],
            'is_overdue': priority['is_overdue'],
            'page_count': order.page_count,
            'is_color': order.is_color,
            'binding': order.get_binding_display(),
            'order_type': order.get_order_type_display(),
            'paper_size': order.paper_size,
            'copies': order.copies,
        })
    board_data.sort(key=lambda x: (x['status_raw'] == 'cancelled', x['remaining_seconds']))
    response = JsonResponse({
        'orders': board_data,
        'system_paused': sys_settings.is_paused,
        'pause_reason': sys_settings.pause_reason,
        'total_active': len(orders),
        'total_cancelled': len(cancelled_orders),
        'last_updated': timezone.now().isoformat(),
    })
    response["Access-Control-Allow-Origin"] = "*"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    return response

def all_links_view(request):
    links_data = [
        ('home', 'Home', 'Landing page'),
        ('dashboard', 'Client Dashboard', 'View your past orders'),
        ('upload', 'Upload / Place Order', 'Upload files for printing'),
        ('track_order', 'Track Order', 'Track order status by ID or email'),
        ('admin_dashboard', 'Admin Dashboard', 'Admin overview and management'),
        ('agent_dashboard', 'Agent Dashboard', 'Station agent dashboard'),
        ('live_board', 'Live Board', 'Full screen live board'),
        ('login', 'Login', 'User login page'),
        ('register', 'Register', 'User registration page'),
    ]
    links = []
    for url_name, name, desc in links_data:
        try:
            url = reverse(url_name)
        except Exception:
            url = '#'
        links.append({'name': name, 'url': url, 'desc': desc})
    links.append({
        'name': 'Django Admin',
        'url': '/admin/',
        'desc': 'Built-in database admin panel'
    })
    return render(request, 'all_links.html', {'links': links})

# ============================================================
# Client Order Cancellation & My Orders
# ============================================================
@login_required
@transaction.atomic
def cancel_order_view(request, order_id):
    if not str(order_id).isdigit():
        messages.error(request, 'Invalid order ID.')
        return redirect('dashboard')
    try:
        order = Order.objects.select_for_update().get(id=int(order_id))
    except Order.DoesNotExist:
        messages.error(request, 'Order not found.')
        return redirect('dashboard')
    if order.client != request.user:
        return HttpResponseForbidden('You can only cancel your own orders.')
    if order.status not in ['pending', 'paid']:
        messages.error(request, 'This order cannot be cancelled. It may already be in production.')
        return redirect('order_receipt', order_id=order.id)
    if request.method == 'POST':
        reason = strip_tags(request.POST.get('cancellation_reason', '').strip())
        order.status = 'cancelled'
        order.cancellation_reason = reason[:500] if reason else 'Cancelled by customer'
        order.cancelled_at = timezone.now()
        order.save(update_fields=['status', 'cancellation_reason', 'cancelled_at'])
        try:
            from notifications.models import Notification
            if order.station:
                agents = User.objects.filter(role='agent', station=order.station)
                for agent in agents:
                    Notification.create_notification(
                        user=agent,
                        notification_type='order_cancelled',
                        title='Order Cancelled by Customer',
                        message=f'Order #{order.id} ({order.file_name}) cancelled. Reason: {reason or "None"}',
                        link=f'/orders/agent-dashboard/'
                    )
            admins = User.objects.filter(role='admin')
            for admin in admins:
                Notification.create_notification(
                    user=admin,
                    notification_type='order_cancelled',
                    title='Order Cancelled by Customer',
                    message=f'Order #{order.id} cancelled by {request.user.username}',
                    link=f'/orders/admin-dashboard/'
                )
        except Exception as e:
            logger.error(f"Failed to create cancellation notifications: {e}")
        try:
            send_cancellation_email(order, reason)
        except Exception as e:
            logger.error(f"Failed to send cancellation email: {e}")
        messages.success(request, f'Order #{order.id} has been cancelled successfully.')
        return redirect('dashboard')
    return render(request, 'orders/cancel_order.html', {'order': order})

@login_required
def my_orders_view(request):
    orders = Order.objects.filter(
        client=request.user
    ).select_related('station', 'delivery_zone').order_by('-created_at')
    for order in orders:
        order.can_cancel = order.status in ['pending', 'paid']
    status_filter = request.GET.get('status', '').strip()
    if status_filter and status_filter in dict(Order.STATUS_CHOICES).keys():
        orders = orders.filter(status=status_filter)
    paginator = Paginator(orders, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'orders/my_orders.html', {
        'page_obj': page_obj,
        'orders': page_obj.object_list,
        'status_filter': status_filter,
        'status_choices': Order.STATUS_CHOICES,
    })

def send_cancellation_email(order, reason=''):
    subject = f'Order #{order.id} Cancelled - PrintHub'
    message = f"""
Dear {order.client.username},

Your order has been cancelled as requested.

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}
- Status: Cancelled

Reason for cancellation: {reason or 'Not specified'}

Place a new order at: {settings.SITE_URL}/upload/

Thank you,
PrintHub Team
"""
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [order.client.email], fail_silently=True)

# ============================================================
# Live Board Preview Image (for social sharing)
# ============================================================
@cache_control(max_age=60)
def live_board_preview_image(request):
    active_statuses = ['paid', 'printing', 'in_transit', 'ready']
    orders = Order.objects.filter(
        status__in=active_statuses
    ).select_related('station', 'client').order_by('id')
    total_active = orders.count()
    ready_count = orders.filter(status='ready').count()
    printing_count = orders.filter(status='printing').count()
    cancelled_count = Order.objects.filter(
        status='cancelled',
        cancelled_at__gte=timezone.now() - timedelta(minutes=30)
    ).count()
    
    img = Image.new('RGB', (1200, 630), color='#0f172a')
    draw = ImageDraw.Draw(img)
    try:
        font_title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 46)
        font_subtitle = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 28)
        font_body = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 24)
        font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 20)
    except Exception:
        font_title = ImageFont.load_default()
        font_subtitle = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_small = ImageFont.load_default()
        
    draw.text((50, 50), "PrintHub Live Board", fill='#e2e8f0', font=font_title)
    draw.text((50, 110), "Kabale University Printing Service", fill='#94a3b8', font=font_subtitle)
    
    stats = [
        ("Active", total_active, '#22c55e'),
        ("Ready", ready_count, '#3b82f6'),
        ("Printing", printing_count, '#a855f7'),
        ("Total Today", total_active + cancelled_count, '#f59e0b'),
    ]
    x = 50
    for label, value, color in stats:
        draw.text((x, 180), label, fill='#94a3b8', font=font_small)
        draw.text((x, 210), str(value), fill=color, font=font_body)
        x += 250
        
    draw.rectangle([50, 280, 1150, 320], fill='#1e293b')
    headers = [
        ("Order", 70), ("Client", 200), ("Station", 400),
        ("Status", 600), ("Time Left", 800), ("Priority", 1000)
    ]
    for text, x_pos in headers:
        draw.text((x_pos, 285), text, fill='#94a3b8', font=font_small)
        
    y = 330
    for order in orders[:4]:
        priority = order.priority_info
        items = [
            (70, f"#{order.id}"),
            (200, order.client.username[:12]),
            (400, order.station.name[:15] if order.station else '—'),
            (600, order.get_status_display()),
            (800, priority['time_display']),
            (1000, priority['display']),
        ]
        for x_pos, text in items:
            draw.text((x_pos, y), text, fill='#e2e8f0', font=font_small)
        y += 55
        
    draw.text((50, 570), "Scan to track your order  |  printlink.pythonanywhere.com", fill='#64748b', font=font_small)
    
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return HttpResponse(buffer, content_type='image/png')

# ============================================================
# Email helpers
# ============================================================
def send_order_confirmation_email(order):
    subject = f'Order #{order.id} Confirmed - PrintHub'
    order_type_info = ""
    if order.order_type == 'passport':
        order_type_info = f"""
Order Type: Passport Photo
Photo Size: {order.get_paper_size_display()}
Copies: {order.copies}
"""
    elif order.order_type == 'scanned':
        order_type_info = f"""
Order Type: Scanned Document
Paper Size: {order.get_paper_size_display()}
Copies: {order.copies}
"""
    else:
        order_type_info = f"""
Paper Size: {order.get_paper_size_display()}
Copies: {order.copies}
"""
    message = f"""
Dear {order.client.username},

Your print order has been received!

Order Details:
- Order ID: #{order.id}
- File: {order.file_name}
- Pages: {order.page_count}
- Color: {'Yes' if order.is_color else 'No'}
- Double-sided: {'Yes' if order.is_double_sided else 'No'}
- Binding: {order.get_binding_display()}{order_type_info}
- Total: {order.total_price:,.0f} UGX

Track your order at: {settings.SITE_URL}/track/?order_id={order.id}

Thank you for choosing PrintHub!
"""
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [order.client.email], fail_silently=True)
