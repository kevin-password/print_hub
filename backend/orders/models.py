# orders/models.py
import math
from datetime import timedelta
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.utils import timezone


class SystemSettings(models.Model):
    """Singleton model to store global system state like pause timers."""
    is_paused = models.BooleanField(default=False)
    pause_reason = models.CharField(max_length=255, blank=True, default='')
    total_paused_seconds = models.FloatField(default=0.0)
    pause_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def get_current_paused_seconds(self):
        total = self.total_paused_seconds
        if self.is_paused and self.pause_started_at:
            total += (timezone.now() - self.pause_started_at).total_seconds()
        return total


class Announcement(models.Model):
    """Custom announcement banner shown at top of all pages."""
    title = models.CharField(max_length=200, default='Announcement')
    message = models.TextField(help_text="Message to display in the announcement bar")
    is_active = models.BooleanField(default=True)
    show_on_home = models.BooleanField(default=True, help_text="Show on homepage")
    background_color = models.CharField(max_length=30, default='bg-blue-600',
        help_text="Tailwind class: bg-blue-600, bg-red-600, bg-green-600, bg-purple-600, bg-orange-600, etc.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Announcement"
        verbose_name_plural = "Announcements"

    def __str__(self):
        return self.message[:60]

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first()


class DeliveryZone(models.Model):
    name = models.CharField(max_length=100, help_text="e.g., Main Campus, City Center")
    description = models.CharField(max_length=255, blank=True, null=True)
    delivery_fee = models.IntegerField(default=0, help_text="Delivery fee in UGX")
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.delivery_fee:,} UGX)"


class Order(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('printing', 'Printing'),
        ('in_transit', 'In Transit'),
        ('ready', 'Ready for Pickup'),
        ('collected', 'Collected'),
        ('cancelled', 'Cancelled'),
    )

    DELIVERY_TYPE_CHOICES = (
        ('pickup', 'Pickup at Station'),
        ('delivery', 'Deliver to Me'),
    )

    BINDING_CHOICES = (
        ('none', 'No Binding'),
        ('staple', 'Staple (Corner)'),
        ('spiral', 'Spiral Binding'),
    )

    ORDER_TYPE_CHOICES = (
        ('document', 'Document Print'),
        ('passport', 'Passport Photo'),
        ('scanned', 'Scanned Document'),
    )

    PAPER_SIZE_CHOICES = (
        ('A4', 'A4'),
        ('4x6', '4x6 (Passport)'),
        ('2x2', '2x2 (Passport)'),
    )

    # Core fields
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    station = models.ForeignKey('stations.Station', on_delete=models.SET_NULL, null=True)
    file = models.FileField(upload_to='print_files/')
    file_name = models.CharField(max_length=255)
    page_count = models.IntegerField()
    is_color = models.BooleanField(default=False)
    is_double_sided = models.BooleanField(default=False)
    binding = models.CharField(max_length=20, choices=BINDING_CHOICES, default='none')
    delivery_type = models.CharField(max_length=20, choices=DELIVERY_TYPE_CHOICES, default='pickup')
    delivery_zone = models.ForeignKey(DeliveryZone, on_delete=models.SET_NULL, null=True, blank=True)

    # Order type, paper size, copies
    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPE_CHOICES,
        default='document',
        help_text="Type of print order (document, passport photo, or scanned document)"
    )
    paper_size = models.CharField(
        max_length=10,
        choices=PAPER_SIZE_CHOICES,
        default='A4',
        help_text="Paper size for printing"
    )
    copies = models.IntegerField(
        default=1,
        help_text="Number of copies to print"
    )

    # Pricing & status
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    transaction_id = models.CharField(max_length=100, blank=True, null=True)
    tx_ref = models.CharField(max_length=100, blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    printing_at = models.DateTimeField(blank=True, null=True)
    in_transit_at = models.DateTimeField(blank=True, null=True)
    ready_at = models.DateTimeField(blank=True, null=True)
    collected_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sla_minutes = models.IntegerField(default=120, help_text="Target time to complete order in minutes.")
    postponed_minutes = models.IntegerField(default=0, help_text="Extra minutes added if the order is postponed.")
    paper_used = models.IntegerField(default=0, help_text="Number of physical sheets consumed")
    cost_of_goods = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Cost of paper used")
    agent_commission = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Commission paid to agent")
    profit = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Net profit for this order")
    notes = models.TextField(blank=True, default='', help_text="Internal notes about this order")
    
    # Referral discount field
    referral_discount_applied = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0,
        help_text="Discount applied from referral bonuses"
    )
    
    cancellation_reason = models.TextField(blank=True, default='', help_text="Reason for cancellation")
    cancelled_at = models.DateTimeField(blank=True, null=True)

    # File processing fields
    file_metadata = models.JSONField(blank=True, default=dict, help_text="File metadata from processing")
    file_preview = models.TextField(blank=True, default='', help_text="Text preview of file content")
    file_thumbnail = models.TextField(blank=True, default='', help_text="Base64 thumbnail for images")

    # Pricing constants
    BASE_PRICE_BW = 200
    COLOR_SURCHARGE = 100
    SPIRAL_BINDING_FEE = 1000
    PASSPORT_PHOTO_PRICE = 1000  # 1000 UGX per photo
    SCANNED_DOC_PRICE = 200

    class Meta:
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['station', 'status']),
            models.Index(fields=['client', 'created_at']),
            models.Index(fields=['created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['order_type']),
            models.Index(fields=['copies']),
            models.Index(fields=['paper_size'])
        ]
        ordering = ['-created_at']

    @classmethod
    def compute_price(cls, page_count, is_color=False, is_double_sided=False, binding='none', delivery_fee=0, order_type='document', paper_size='A4', copies=1):
        """
        Calculate total price based on order type and options.
        Returns: (total_price, effective_pages, price_per_unit)
        """
        if order_type == 'passport':
            price_per_unit = cls.PASSPORT_PHOTO_PRICE
            printing_cost = price_per_unit * copies
            total_price = printing_cost + delivery_fee
            return total_price, copies, price_per_unit

        elif order_type == 'scanned':
            price_per_unit = cls.SCANNED_DOC_PRICE + (cls.COLOR_SURCHARGE if is_color else 0)
            effective_pages = page_count
            if is_double_sided:
                effective_pages = max(1, math.ceil(page_count / 2))
            printing_cost = price_per_unit * effective_pages * copies
            binding_cost = cls.SPIRAL_BINDING_FEE if binding == 'spiral' else 0
            total_price = printing_cost + binding_cost + delivery_fee
            return total_price, effective_pages * copies, price_per_unit

        else:
            price_per_unit = cls.BASE_PRICE_BW + (cls.COLOR_SURCHARGE if is_color else 0)
            effective_pages = page_count
            if is_double_sided:
                effective_pages = max(1, math.ceil(page_count / 2))
            printing_cost = price_per_unit * effective_pages * copies
            binding_cost = cls.SPIRAL_BINDING_FEE if binding == 'spiral' else 0
            total_price = printing_cost + binding_cost + delivery_fee
            return total_price, effective_pages * copies, price_per_unit

    def calculate_price(self):
        """Calculate and set the total price for this order."""
        delivery_fee = self.delivery_zone.delivery_fee if self.delivery_zone and self.delivery_type == 'delivery' else 0
        total, effective_pages, price_per_page = self.compute_price(
            self.page_count,
            self.is_color,
            self.is_double_sided,
            self.binding,
            delivery_fee,
            self.order_type,
            self.paper_size,
            self.copies
        )
        self.total_price = Decimal(str(total))
        return self.total_price, effective_pages, price_per_page

    def calculate_financials(self):
        """Calculate cost of goods, commission, and profit."""
        _, effective_pages, _ = self.compute_price(
            self.page_count,
            self.is_color,
            self.is_double_sided,
            self.binding,
            0,
            self.order_type,
            self.paper_size,
            self.copies
        )
        self.paper_used = effective_pages
        try:
            from finances.models import PaperInventory
            paper = PaperInventory.objects.first()
            self.cost_of_goods = Decimal(str(effective_pages * paper.cost_per_sheet)) if paper else Decimal('0.00')
        except Exception:
            self.cost_of_goods = Decimal('0.00')

        try:
            from finances.models import CommissionRate
            rate = CommissionRate.get_active_rate()
            self.agent_commission = (self.total_price * Decimal(str(rate.rate_percentage))) / Decimal('100') if rate else Decimal('0.00')
        except Exception:
            self.agent_commission = Decimal('0.00')

        self.profit = self.total_price - self.cost_of_goods - self.agent_commission
        return self.profit

    def deduct_paper_inventory(self):
        """Deduct paper from inventory when printing starts."""
        if self.status == 'printing' and self.paper_used > 0:
            try:
                from finances.models import PaperInventory
                paper = PaperInventory.objects.first()
                if paper and paper.quantity >= self.paper_used:
                    paper.quantity -= self.paper_used
                    paper.save(update_fields=['quantity'])
                    return True
            except Exception:
                pass
        return False

    def estimated_ready_at(self):
        """Calculate estimated completion time."""
        if self.paid_at:
            total_minutes = self.sla_minutes + self.postponed_minutes
            return self.paid_at + timedelta(minutes=total_minutes)
        return None

    @property
    def price_per_unit(self):
        """Get the price per unit based on order type."""
        if self.order_type == 'passport':
            return self.PASSPORT_PHOTO_PRICE
        elif self.order_type == 'scanned':
            return self.SCANNED_DOC_PRICE + (self.COLOR_SURCHARGE if self.is_color else 0)
        else:
            return self.BASE_PRICE_BW + (self.COLOR_SURCHARGE if self.is_color else 0)

    @property
    def unit_label(self):
        """Get the unit label for display (page, copy, sheet)."""
        if self.order_type == 'passport':
            return 'copy'
        return 'page'

    @property
    def can_be_cancelled(self):
        """Check if order can be cancelled by client."""
        return self.status in ['pending', 'paid'] and not self.printing_at

    @property
    def is_overdue(self):
        """Check if order is past its estimated completion time."""
        if self.status in ['collected', 'cancelled']:
            return False
        estimated = self.estimated_ready_at()
        if estimated:
            return timezone.now() > estimated
        return False

    @property
    def priority_info(self):
        """Get priority information for live board display."""
        if self.status == 'cancelled':
            return {
                'level': 'cancelled', 'display': 'CANCELLED',
                'remaining_seconds': 0, 'time_display': '--:--:--', 'is_overdue': False
            }

        start_time = self.paid_at or self.created_at
        total_minutes = self.sla_minutes + self.postponed_minutes
        deadline = start_time + timedelta(minutes=total_minutes)
        now = timezone.now()

        try:
            sys_settings = SystemSettings.load()
            paused_seconds = sys_settings.get_current_paused_seconds()
        except Exception:
            paused_seconds = 0

        effective_deadline = deadline + timedelta(seconds=paused_seconds)
        remaining_td = effective_deadline - now
        remaining_seconds = max(0, int(remaining_td.total_seconds()))
        is_overdue = now > effective_deadline
        is_postponed = self.postponed_minutes > 0

        if is_postponed:
            level, display = 'postponed', 'POSTPONED'
        elif is_overdue:
            level, display = 'overdue', 'OVERDUE'
        elif remaining_seconds < 600:
            level, display = 'critical', 'CRITICAL'
        elif remaining_seconds < 1800:
            level, display = 'urgent', 'URGENT'
        elif remaining_seconds < 3600:
            level, display = 'high', 'HIGH'
        else:
            level, display = 'normal', 'NORMAL'

        hours, remainder = divmod(remaining_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        time_display = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        return {
            'level': level, 'display': display,
            'remaining_seconds': remaining_seconds,
            'time_display': time_display, 'is_overdue': is_overdue
        }

    @property
    def is_passport_photo(self):
        """Check if this is a passport photo order."""
        return self.order_type == 'passport'

    @property
    def is_scanned_document(self):
        """Check if this is a scanned document order."""
        return self.order_type == 'scanned'

    @property
    def total_sheets(self):
        """Calculate total physical sheets needed."""
        _, effective_pages, _ = self.compute_price(
            self.page_count, self.is_color, self.is_double_sided,
            self.binding, 0, self.order_type, self.paper_size, self.copies
        )
        return effective_pages

    @property
    def effective_page_count(self):
        """Get effective page count for display."""
        if self.order_type == 'passport':
            return self.copies
        return self.page_count

    def save(self, *args, **kwargs):
        is_new = self._state.adding

        # ONLY calculate price if total_price is 0 (JavaScript/View didn't provide one)
        if self.total_price == 0:
            self.calculate_price()

        # Track old status for signal handling
        if not is_new:
            try:
                old_instance = Order.objects.get(pk=self.pk)
                self._old_status = old_instance.status
            except Order.DoesNotExist:
                self._old_status = None
        else:
            self._old_status = None

        # Set cancelled timestamp
        if self.status == 'cancelled' and not self.cancelled_at:
            self.cancelled_at = timezone.now()

        super().save(*args, **kwargs)

    def __str__(self):
        order_type_display = dict(self.ORDER_TYPE_CHOICES).get(self.order_type, 'Document')
        return f"{order_type_display} Order #{self.id} by {self.client.username}"


class SupportSettings(models.Model):
    """Support settings for the chatbot."""
    support_group_link = models.URLField(
        max_length=500,
        blank=True,
        help_text="WhatsApp group invite link (e.g., https://chat.whatsapp.com/...)"
    )
    support_admin_number = models.CharField(
        max_length=15,
        blank=True,
        help_text="Default admin WhatsApp number if no admin found"
    )
    support_agent_fallback = models.CharField(
        max_length=15,
        blank=True,
        help_text="Fallback agent WhatsApp number if none assigned"
    )
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Support Setting"
        verbose_name_plural = "Support Settings"
    
    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
    
    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


# ============================================================
# DOCUMENT CREATION SERVICE MODELS
# ============================================================

class DocumentCreationRequest(models.Model):
    DOCUMENT_TYPES = [
        ('research_proposal', 'Research Proposal'),
        ('essay', 'Essay/Assignment'),
        ('report', 'Technical Report'),
        ('cv_resume', 'CV/Resume'),
        ('business_plan', 'Business Plan'),
        ('presentation', 'Presentation Slides'),
        ('thesis_chapter', 'Thesis Chapter'),
        ('other', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('research', 'Research Phase (NotebookLM)'),
        ('generating', 'AI Generating Draft'),
        ('formatting', 'Formatting to LaTeX'),
        ('human_review', 'Team Review'),
        ('client_review', 'Awaiting Client Review'),
        ('revision', 'Revisions Requested'),
        ('approved', 'Final Approval'),
        ('printing', 'Ready to Print'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
    ]
    
    # FIXED: Use settings.AUTH_USER_MODEL instead of User
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='doc_requests')
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPES)
    title = models.CharField(max_length=300)
    
    description = models.TextField(help_text="What do you need?")
    instructions = models.TextField(help_text="Specific requirements (formatting, style, references)")
    word_count_target = models.IntegerField(default=1000)
    deadline = models.DateTimeField()
    
    estimated_hours = models.DecimalField(max_digits=4, decimal_places=1, default=0)
    
    notebooklm_link = models.URLField(blank=True, help_text="Link to NotebookLM project")
    research_notes = models.TextField(blank=True, help_text="Extracted notes from NotebookLM")
    
    ai_model_used = models.CharField(max_length=50, blank=True)
    ai_draft_text = models.TextField(blank=True)
    ai_draft_tokens = models.IntegerField(default=0)
    
    latex_code = models.TextField(blank=True)
    overleaf_project_url = models.URLField(blank=True)
    final_pdf = models.FileField(upload_to='final_documents/%Y/%m/', blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # FIXED: Use settings.AUTH_USER_MODEL instead of User
    assigned_team_member = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='assigned_doc_requests'
    )
    
    revision_count = models.IntegerField(default=0)
    max_revisions = models.IntegerField(default=1)
    
    linked_order = models.OneToOneField('Order', on_delete=models.SET_NULL, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.title} - {self.client.username}"
    
    def can_request_revision(self):
        return self.revision_count < self.max_revisions and self.status in ['client_review', 'revision']
    
    def get_status_color(self):
        colors = {
            'pending': 'text-gray-400',
            'research': 'text-blue-400',
            'generating': 'text-purple-400',
            'formatting': 'text-indigo-400',
            'human_review': 'text-yellow-400',
            'client_review': 'text-cyan-400',
            'revision': 'text-orange-400',
            'approved': 'text-green-400',
            'printing': 'text-blue-300',
            'completed': 'text-green-300',
            'rejected': 'text-red-400',
        }
        return colors.get(self.status, 'text-gray-400')


class DocumentSourceFile(models.Model):
    request = models.ForeignKey(DocumentCreationRequest, on_delete=models.CASCADE, related_name='source_files')
    file = models.FileField(upload_to='doc_sources/%Y/%m/')
    file_name = models.CharField(max_length=255)
    file_size = models.IntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.file_name


class DocumentRevision(models.Model):
    request = models.ForeignKey(DocumentCreationRequest, on_delete=models.CASCADE, related_name='revisions')
    client_notes = models.TextField()
    team_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Revision for {self.request.title} ({self.created_at})"
