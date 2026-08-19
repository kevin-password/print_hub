# orders/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from decimal import Decimal
import logging

from .models import Order

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Order)
def handle_order_status_change(sender, instance, created, **kwargs):
    """
    Handle side effects when order status changes:
    - Set timestamps for status changes
    - Deduct paper inventory when printing
    - Create financial records when collected
    - Create notifications for status changes
    """
    
    if created:
        # 🚨 NEW: Send Discord alert to agents for new orders
        try:
            from core.discord_utils import send_discord_alert
            send_discord_alert(
                title="🖨️ New Order Received!",
                message=(
                    f"**Order ID:** #{instance.id}\n"
                    f"**Customer:** {instance.client.get_full_name() or instance.client.username}\n"
                    f"**File:** {instance.file_name}\n"
                    f"**Total:** UGX {instance.total_price:,.0f}"
                ),
                target="agent"
            )
        except Exception as e:
            logger.warning(f"Discord notification failed for new order: {e}")
        return
    
    old_status = getattr(instance, '_old_status', None)
    
    if old_status == instance.status:
        return
    
    now = timezone.now()
    
    if instance.status == 'paid' and not instance.paid_at:
        instance.paid_at = now
        instance.save(update_fields=['paid_at'])
    
    elif instance.status == 'printing':
        if not instance.printing_at:
            instance.printing_at = now
            instance.save(update_fields=['printing_at'])
        instance.deduct_paper_inventory()
    
    elif instance.status == 'in_transit' and not instance.in_transit_at:
        instance.in_transit_at = now
        instance.save(update_fields=['in_transit_at'])
    
    elif instance.status == 'ready' and not instance.ready_at:
        instance.ready_at = now
        instance.save(update_fields=['ready_at'])
        create_order_notification(instance, 'ready')
    
    elif instance.status == 'collected':
        if not instance.collected_at:
            instance.collected_at = now
        instance.calculate_financials()
        instance.save(update_fields=['paper_used', 'cost_of_goods', 'agent_commission', 'profit', 'collected_at'])
        create_financial_records(instance)
    
    elif instance.status == 'cancelled':
        create_order_notification(instance, 'cancelled')

    if not created and old_status != instance.status:
        try:
            from whatsapp_bot.views import send_whatsapp_message
            phone = instance.client.phone_number
            if phone:
                status_emoji = {
                    'paid': '💳',
                    'printing': '🖨️',
                    'ready': '✅',
                    'collected': '📦',
                    'cancelled': '❌'
                }.get(instance.status, '📋')

                send_whatsapp_message(
                    phone,
                    f"{status_emoji} *order #{instance.id} update*\n\n"
                    f"Status: *{instance.get_status_display()}*\n"
                    f"File: {instance.file_name}\n"
                    f"Total: {instance.total_price:,.0f} UGX\n\n"
                    f"Track: https://printlink.pythonanywhere.com/track/?order_id={instance.id}"
                )
        except Exception as e:
            logger.warning(f"WhatsApp notification failed: {e}")   
                    


def create_order_notification(order, status_type):
    """Create notification for order status changes."""
    try:
        from notifications.models import Notification
        
        notifications_map = {
            'paid': {
                'title': 'Payment Confirmed',
                'message': f'Payment received for Order #{order.id}. Your order is being processed.',
            },
            '
