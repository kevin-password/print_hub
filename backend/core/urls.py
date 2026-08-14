# backend/core/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.generic import TemplateView
from django.contrib.sitemaps.views import sitemap
from django.views.static import serve

from orders.views import (
    client_views, admin_views, agent_views, 
    api_views, live_board_views, document_creation_views
)
from accounts import views as accounts_views
from .sitemap import StaticSitemap


# ─── ROBOTS.TXT ──────────────────────────────────────────────
def robots_txt(request):
    lines = [
        "User-agent: *",
        "Allow: /",
        "Allow: /upload/",
        "Allow: /dashboard/",
        "Allow: /track/",
        "Allow: /live-board/",
        "Allow: /stations/",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /accounts/",
        "Disallow: /payments/",
        "Disallow: /whatsapp/",
        "Disallow: /webhook/",
        "Sitemap: https://www.printhubug.com/sitemap.xml",
        "Crawl-delay: 2",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


# ─── SITEMAP - STATIC FILE (NO DJANGO FRAMEWORK) ───────────
def static_sitemap(request):
    """Serve static sitemap.xml with forced index header."""
    import os
    from django.conf import settings
    
    file_path = os.path.join(settings.BASE_DIR, 'static', 'sitemap.xml')
    
    with open(file_path, 'r') as f:
        content = f.read()
    
    response = HttpResponse(content, content_type='application/xml')
    
    # 🔥 FORCE INDEX - No middleware can override this
    response['X-Robots-Tag'] = 'index, follow'
    
    # Remove any other headers that might cause issues
    response['Cache-Control'] = 'public, max-age=3600'
    
    return response


# ============================================================
# URL PATTERNS
# ============================================================
urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),
    
    # Home
    path('', client_views.home_view, name='home'),
    
    # Authentication
    path('auth/login/', accounts_views.login_view, name='login'),
    path('auth/logout/', accounts_views.logout_view, name='logout'),
    path('auth/register/', accounts_views.register_view, name='register'),
    path('auth/profile/', accounts_views.profile_view, name='profile'),
    path('auth/', include('django.contrib.auth.urls')),
    
    # Orders & Upload
    path('dashboard/', client_views.dashboard_view, name='dashboard'),
    path('upload/', client_views.upload_view, name='upload'),
    path('my-orders/', client_views.my_orders_view, name='my_orders'),
    path('track/', client_views.order_track_view, name='track_order'),
    
    # Order Details
    path('orders/<int:order_id>/receipt/', client_views.order_receipt_view, name='order_receipt'),
    path('orders/<int:order_id>/passport-receipt/', client_views.passport_receipt_view, name='passport_receipt'),
    path('orders/<int:order_id>/payment/', client_views.payment_page_view, name='payment_page'),
    path('orders/<int:order_id>/cancel/', client_views.cancel_order_view, name='cancel_order'),
    path('orders/<int:order_id>/download/', client_views.download_order_file_view, name='download_order_file'),
    path('orders/<int:order_id>/update-status/', agent_views.update_order_status_view, name='update_order_status'),
    
    # Admin & Agent
    path('orders/admin-dashboard/', admin_views.admin_dashboard_view, name='admin_dashboard'),
    path('orders/agent-dashboard/', agent_views.agent_dashboard_view, name='agent_dashboard'),
    path('orders/toggle-system-pause/', admin_views.toggle_system_pause_view, name='toggle_system_pause'),
    
    # Live Board
    path('live-board/', live_board_views.live_board_view, name='live_board'),
    path('api/live-board/', live_board_views.live_board_api_view, name='live_board_api'),
    path('api/live-board/preview/', live_board_views.live_board_preview_image, name='live_board_preview'),
    
    # API
    path('orders/api/analyze-passport/', login_required(api_views.api_analyze_passport), name='analyze_passport'),
    path('orders/api/process-passport/', login_required(api_views.api_process_passport), name='process_passport'),
    path('orders/api/process-scan/', login_required(api_views.api_process_scan), name='process_scan'),
    path('orders/api/validate-discount/', api_views.validate_discount_code, name='validate_discount_code'),

        # Kabale landing page
    path('kabale/', TemplateView.as_view(template_name='kabale.html'), name='kabale'),
    
    # Assistant
    path('api/assistant/', include('assistant.urls')),
    
    # ─── SEO URLs ────────────────────────────────────────────
    path('robots.txt', robots_txt, name='robots'),
    
    # 🔥 USE STATIC SITEMAP - Bypasses all middleware
    path('sitemap.xml', static_sitemap, name='sitemap'),

    
    
    # Misc
    path('all-links/', client_views.all_links_view, name='all_links'),
]

# Include other apps
urlpatterns += [path('finances/', include('finances.urls'))]
urlpatterns += [path('payments/', include('payments.urls'))]
urlpatterns += [path('notifications/', include('notifications.urls'))]
urlpatterns += [path('stations/', include('stations.urls'))]
urlpatterns += [path('referrals/', include('referrals.urls'))]


    # Document Creation Service
    path('documents/create/', document_creation_views.create_document_request_view, name='create_document_request'),
    path('documents/<int:request_id>/', document_creation_views.doc_request_detail_view, name='doc_request_detail'),
    path('documents/admin/', document_creation_views.admin_doc_requests_view, name='admin_doc_requests'),
    path('documents/my-requests/', document_creation_views.my_doc_requests_view, name='my_doc_requests'),



# Static & Media (Development)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
