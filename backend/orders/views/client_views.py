# backend/orders/views/client_views.py
import json
import logging
import base64
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.core.validators import ValidationError
from django.core.mail import send_mail
from django.conf import settings
from django.urls import reverse
from django.utils.html import strip_tags

from stations.models import Station
from orders.models import Order, DeliveryZone, Announcement
from orders.utils import apply_order_status_change
from .helpers import (
    _user_role, _can_view_order, validate_upload_file,
    _build_order_queryset, _order_summary_counts,
    _get_tracked_orders, send_order_confirmation_email, send_cancellation_email
)

# ============================================================
# 🚫 FILE PROCESSOR - COMPLETELY DISABLED
# ============================================================
# The file_processor app is disabled for local testing and Render deployment.
# Set FileProcessor to None so all checks pass without errors.
FileProcessor = None
print("⚠️ file_processor is DISABLED - file processing skipped")

User = get_user_model()
logger = logging.getLogger(__name__)


# ============================================================
# DASHBOARD VIEW
# ============================================================
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


# ============================================================
# UPLOAD VIEW - WITH FILE PROCESSOR DISABLED
# ============================================================
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
        
        order_type = request.POST.get('order_type', 'document')
        paper_size = request.POST.get('paper_size', 'A4')
        copies = request.POST.get('copies', 1)
        calculated_price = request.POST.get('calculated_price', '0')
        
        passport_data = request.POST.get('passport_data', '')
        scanner_data = request.POST.get('scanner_data', '')
        
        # ============================================================
        # FORCE: order_type based on data presence
        # ============================================================
        if passport_data:
            order_type = 'passport'
            logger.info(f"📸 FORCED order_type to 'passport' because passport_data exists")
        elif scanner_data:
            order_type = 'scanned'
            logger.info(f"📄 FORCED order_type to 'scanned' because scanner_data exists")
        
        if not file and (passport_data or scanner_data):
            try:
                if passport_data:
                    format, imgstr = passport_data.split(';base64,')
                    ext = format.split('/')[-1]
                    file = ContentFile(
                        base64.b64decode(imgstr),
                        name=f'passport_photo.{ext}'
                    )
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
        
        # ============================================================
        # 🚫 FILE PROCESSING - SKIPPED (FileProcessor is disabled)
        # ============================================================
        processing_result = None
        # FileProcessor is disabled - skipping file processing
        logger.info("📄 File processing skipped (FileProcessor disabled)")
            
        station = None
        if station_id and station_id.isdigit():
            station = Station.objects.filter(id=int(station_id)).first()
            
        delivery_zone = None
        if delivery_type == 'delivery' and delivery_zone_id and delivery_zone_id.isdigit():
            delivery_zone = DeliveryZone.objects.filter(id=int(delivery_zone_id)).first()
            
        try:
            page_count_int = int(page_count)
            copies_int = int(copies)
            
            # ============================================================
            # FORCE: If passport and copies is less than 6, force to 6
            # ============================================================
            if order_type == 'passport':
                if copies_int < 6:
                    copies_int = 6
                    logger.warning(f"📸 FORCED passport copies from {copies} to 6")
            
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

            # ============================================================
            # PASSPORT: Let the model calculate price
            # ============================================================
            if order_type == 'passport':
                is_color = True
                binding = 'none'
                is_double_sided = False
                page_count_int = copies_int
                calculated_price = '0'
                logger.info(f"📸 PASSPORT ORDER: copies={copies_int}, page_count={page_count_int}")
                
            elif order_type == 'scanned':
                binding = 'none'
                is_double_sided = False
                page_count_int = copies_int if copies_int > page_count_int else page_count_int

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
            
            # Skip file metadata processing (FileProcessor disabled)
            # order.file_metadata, order.file_preview, order.file_thumbnail remain empty
            
            # Only set total_price if calculated_price is NOT 0
            if calculated_price and calculated_price != '0':
                try:
                    js_price = Decimal(str(calculated_price))
                    if js_price > 0:
                        order.total_price = js_price
                except Exception:
                    pass
                    
            order.save()
            
            logger.info(f"✅ ORDER SAVED: order #{order.id}, copies={order.copies}, page_count={order.page_count}, total_price={order.total_price}")
            
            try:
                send_order_confirmation_email(order)
            except Exception as e:
                logger.error(f"Failed to send confirmation email for order #{order.id}: {e}", exc_info=True)
                
            messages.success(request, f'Order #{order.id} submitted! Total: {order.total_price:,.0f} UGX')
            
            # Redirect based on order type
            if order.order_type == 'passport':
                return redirect('passport_receipt', order_id=order.id)
            else:
                return redirect('order_receipt', order_id=order.id)
            
        except ValueError as e:
            upload_error = f'Invalid input: {str(e)}'
        except Exception as e:
            logger.error(f"Error creating order: {e}", exc_info=True)
            upload_error = 'Error creating order. Please try again.'
            
        return render(request, 'orders/upload.html', {
            'stations': stations,
            'delivery_zones': delivery_zones,
            'upload_error': upload_error,
        })

    return render(request, 'orders/upload.html', {
        'stations': stations,
        'delivery_zones': delivery_zones,
        'upload_error': upload_error,
    })


# ============================================================
# PASSPORT RECEIPT VIEW
# ============================================================
@login_required
def passport_receipt_view(request, order_id):
    """Direct passport receipt view"""
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
    
    return render(request, 'orders/receipt_passport.html', {
        'order': order,
        'estimated_ready': estimated_ready,
        'payment': payment,
    })


# ============================================================
# ORDER RECEIPT VIEW
# ============================================================
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


# ============================================================
# CANCEL ORDER VIEW
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
        messages.error(request, 'This order cannot be cancelled.')
        return redirect('order_receipt', order_id=order.id)
    if request.method == 'POST':
        reason = strip_tags(request.POST.get('cancellation_reason', '').strip())
        order.status = 'cancelled'
        order.cancellation_reason = reason[:500] if reason else 'Cancelled by customer'
        order.cancelled_at = timezone.now()
        order.save(update_fields=['status', 'cancellation_reason', 'cancelled_at'])
        messages.success(request, f'Order #{order.id} has been cancelled.')
        return redirect('dashboard')
    return render(request, 'orders/cancel_order.html', {'order': order})


# ============================================================
# MY ORDERS VIEW
# ============================================================
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


# ============================================================
# DOWNLOAD ORDER FILE VIEW (FIXED)
# ============================================================
@login_required
def download_order_file_view(request, order_id):
    """
    FIX: Redirects directly to the Cloudinary URL instead of streaming
    through the server. This bypasses the 401 Unauthorized error and 
    saves Render server bandwidth. ?fl_attachment=true forces download.
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
        
    # 🛡️ Redirect directly to Cloudinary URL to avoid 401 errors
    download_url = f"{order.file.url}?fl_attachment=true"
    
    return redirect(download_url)


# ============================================================
# PAYMENT PAGE VIEW
# ============================================================
@login_required
def payment_page_view(request, order_id):
    if not str(order_id).isdigit():
        return HttpResponseForbidden('Invalid order ID.')
    order = get_object_or_404(Order.objects.select_related('station', 'delivery_zone'), id=int(order_id))
    if order.client != request.user:
        return HttpResponseForbidden('You can only pay for your own orders.')
    if order.status != 'pending':
        messages.info(request, 'This order has already been paid or is being processed.')
        return redirect('order_receipt', order_id=order.id)
    return render(request, 'orders/payment.html', {'order': order})


# ============================================================
# TRACK ORDER VIEW
# ============================================================
def order_track_view(request):
    orders = None
    lookup_error = None
    order_id = request.GET.get('order_id', '').strip() or request.POST.get('order_id', '').strip()
    email = request.GET.get('email', '').strip() or request.POST.get('email', '').strip()
    
    if order_id or email:
        if order_id:
            if str(order_id).isdigit():
                orders = Order.objects.select_related('station', 'client', 'delivery_zone').filter(id=int(order_id))
            if not orders or not orders.exists():
                lookup_error = 'No order found with that order ID.'
                orders = None
        elif email:
            try:
                from django.core.validators import validate_email
                validate_email(email)
                orders = Order.objects.filter(client__email__iexact=email).select_related('station', 'client', 'delivery_zone').order_by('-created_at')
                if not orders.exists():
                    lookup_error = 'No orders found for that email address.'
                    orders = None
            except ValidationError:
                lookup_error = 'Invalid email address.'
    
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


# ============================================================
# HOME VIEW
# ============================================================
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


# ============================================================
# ALL LINKS VIEW
# ============================================================
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
