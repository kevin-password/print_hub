# backend/payments/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Payment
from core.discord_utils import send_discord_alert

@receiver(post_save, sender=Payment)
def notify_payment_status_change(sender, instance, created, **kwargs):
    """Alert team when payment status changes."""
    
    if created and instance.status == 'pending':
        send_discord_alert(
            title="💰 Payment Pending Verification",
            message=(
                f"**Order ID:** #{instance.order.id}\n"
                f"**Customer:** {instance.customer_name or instance.user.username}\n"
                f"**Method:** {instance.get_payment_method_display()}\n"
                f"**Amount:** UGX {instance.amount:,.0f}\n"
                f"**Transaction ID:** `{instance.transaction_id}`\n"
                f"**Action:** Approve or Reject in Admin Dashboard"
            ),
            target="admin"
        )
    elif not created:
        # Triggered on status update
        if instance.status == 'approved':
            send_discord_alert(
                title="✅ Payment Approved",
                message=(
                    f"**Order ID:** #{instance.order.id}\n"
                    f"**Amount:** UGX {instance.amount:,.0f}\n"
                    f"**Transaction ID:** `{instance.transaction_id}`\n"
                    f"**Approved by:** {instance.approved_by.username if instance.approved_by else 'System'}"
                ),
                target="agent"  # Agents need to know it's paid so they can print
            )
        elif instance.status == 'rejected':
            send_discord_alert(
                title="❌ Payment Rejected",
                message=(
                    f"**Order ID:** #{instance.order.id}\n"
                    f"**Amount:** UGX {instance.amount:,.0f}\n"
                    f"**Reason:** {instance.status_reason or 'No reason provided'}"
                ),
                target="admin"
            )
