# orders/urls.py
from django.urls import path
from .views import client_views, admin_views, agent_views, api_views, live_board_views, document_views

urlpatterns = [
    # Public URLs
    path('', client_views.home_view, name='home'),
    path('track/', client_views.order_track_view, name='track_order'),
    path('links/', client_views.all_links_view, name='all_links'),
    
    # Client URLs
    path('dashboard/', client_views.dashboard_view, name='dashboard'),
    path('upload/', client_views.upload_view, name='upload'),
    path('my-orders/', client_views.my_orders_view, name='my_orders'),
    path('<int:order_id>/receipt/', client_views.order_receipt_view, name='order_receipt'),
    path('<int:order_id>/cancel/', client_views.cancel_order_view, name='cancel_order'),
    path('<int:order_id>/download/', client_views.download_order_file_view, name='download_order_file'),
    path('<int:order_id>/payment/', client_views.payment_page_view, name='payment_page'),
    
    # Document Creation Service
    path('documents/create/', document_views.create_document_request_view, name='create_document_request'),
    path('documents/<int:request_id>/', document_views.doc_request_detail_view, name='doc_request_detail'),
    path('documents/admin/', document_views.admin_doc_requests_view, name='admin_doc_requests'),
    path('documents/my-requests/', document_views.my_doc_requests_view, name='my_doc_requests'),
    
    # Admin URLs
    path('admin-dashboard/', admin_views.admin_dashboard_view, name='admin_dashboard'),
    path('toggle-pause/', admin_views.toggle_system_pause_view, name='toggle_system_pause'),
    
    # Agent URLs
    path('agent-dashboard/', agent_views.agent_dashboard_view, name='agent_dashboard'),
    path('<int:order_id>/update-status/', agent_views.update_order_status_view, name='update_order_status'),
    
    # Live Board URLs
    path('live-board/', live_board_views.live_board_view, name='live_board'),
    path('api/live-board/', live_board_views.live_board_api_view, name='live_board_api'),
    path('live-board-preview/', live_board_views.live_board_preview_image, name='live_board_preview'),
    
    # API Endpoints
    path('api/analyze-passport/', api_views.api_analyze_passport, name='api_analyze_passport'),
    path('api/process-passport/', api_views.api_process_passport, name='api_process_passport'),
    path('api/process-scan/', api_views.api_process_scan, name='api_process_scan'),
    path('api/validate-discount/', api_views.validate_discount_code, name='validate_discount_code'),
]
