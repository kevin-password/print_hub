# backend/orders/views/document_creation_views.py

import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone

# ✅ FIXED: Use absolute import path instead of relative
from orders.models import (
    DocumentCreationRequest, 
    DocumentSourceFile, 
    DocumentRevision
)
from orders.services.ai_document_service import FreeAIDocumentService

logger = logging.getLogger(__name__)


# ============================================================
# CLIENT: CREATE DOCUMENT REQUEST
# ============================================================
@login_required
def create_document_request_view(request):
    """Client submits a new document creation request"""
    
    if request.method == 'POST':
        try:
            with transaction.atomic():
                doc_request = DocumentCreationRequest.objects.create(
                    client=request.user,
                    document_type=request.POST.get('document_type'),
                    title=request.POST.get('title', '').strip(),
                    description=request.POST.get('description', '').strip(),
                    instructions=request.POST.get('instructions', '').strip(),
                    word_count_target=int(request.POST.get('word_count_target', 1000)),
                    deadline=request.POST.get('deadline'),
                    status='pending'
                )
                
                # Handle source file uploads
                for file in request.FILES.getlist('source_files'):
                    DocumentSourceFile.objects.create(
                        request=doc_request,
                        file=file,
                        file_name=file.name,
                        file_size=file.size
                    )
                
                messages.success(
                    request, 
                    f'✅ Request "{doc_request.title}" submitted! Our team will start working on it soon.'
                )
                return redirect('doc_request_detail', request_id=doc_request.id)
                
        except Exception as e:
            logger.error(f"Error creating document request: {e}")
            messages.error(request, f'Error: {str(e)}')
    
    return render(request, 'orders/create_document_request.html', {
        'document_types': DocumentCreationRequest.DOCUMENT_TYPES,
    })


# ============================================================
# CLIENT/TEAM: VIEW REQUEST DETAIL
# ============================================================
@login_required
def doc_request_detail_view(request, request_id):
    """View document request details (client or team member)"""
    
    doc_request = get_object_or_404(DocumentCreationRequest, id=request_id)
    
    # Check permissions
    is_client = (doc_request.client == request.user)
    is_team = getattr(request.user, 'role', '') in ['admin', 'agent'] or request.user.is_staff
    
    if not (is_client or is_team):
        messages.error(request, 'Access denied.')
        return redirect('dashboard')
    
    if request.method == 'POST':
        # Client: Request revision
        if is_client and doc_request.can_request_revision() and 'request_revision' in request.POST:
            notes = request.POST.get('revision_notes', '').strip()
            if notes:
                DocumentRevision.objects.create(request=doc_request, client_notes=notes)
                doc_request.revision_count += 1
                doc_request.status = 'revision'
                doc_request.save()
                messages.success(request, '✅ Revision request submitted!')
                return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Client: Approve
        if is_client and doc_request.status == 'client_review' and 'approve' in request.POST:
            doc_request.status = 'approved'
            doc_request.save()
            messages.success(request, '✅ Approved! Preparing for printing.')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Save research notes
        if is_team and 'save_research' in request.POST:
            doc_request.research_notes = request.POST.get('research_notes', '')
            doc_request.notebooklm_link = request.POST.get('notebooklm_link', '')
            doc_request.status = 'generating'
            doc_request.save()
            messages.success(request, '✅ Research saved! Ready to generate draft.')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Generate draft
        if is_team and 'generate_draft' in request.POST:
            try:
                ai_service = FreeAIDocumentService()
                draft = ai_service.generate_document_draft(doc_request)
                doc_request.ai_draft_text = draft
                doc_request.status = 'formatting'
                doc_request.save()
                messages.success(request, '✅ AI draft generated!')
            except Exception as e:
                messages.error(request, f'AI failed: {str(e)}')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Format LaTeX
        if is_team and 'format_latex' in request.POST:
            try:
                ai_service = FreeAIDocumentService()
                latex = ai_service.format_to_latex(doc_request.ai_draft_text, doc_request.get_document_type_display())
                doc_request.latex_code = latex
                doc_request.status = 'human_review'
                doc_request.save()
                messages.success(request, '✅ LaTeX generated! Review and create Overleaf project.')
            except Exception as e:
                messages.error(request, f'LaTeX failed: {str(e)}')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Send to client
        if is_team and 'send_to_client' in request.POST:
            overleaf_url = request.POST.get('overleaf_url', '').strip()
            if overleaf_url:
                doc_request.overleaf_project_url = overleaf_url
            doc_request.status = 'client_review'
            doc_request.save()
            messages.success(request, '✅ Sent to client!')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Mark as printing
        if is_team and 'mark_printing' in request.POST:
            doc_request.status = 'printing'
            doc_request.save()
            messages.success(request, '✅ Ready for printing!')
            return redirect('doc_request_detail', request_id=doc_request.id)
        
        # Team: Mark completed
        if is_team and 'mark_completed' in request.POST:
            doc_request.status = 'completed'
            doc_request.save()
            messages.success(request, '✅ Marked as completed!')
            return redirect('doc_request_detail', request_id=doc_request.id)
    
    return render(request, 'orders/doc_request_detail.html', {
        'doc_request': doc_request,
        'is_client': is_client,
        'is_team': is_team,
        'source_files': doc_request.source_files.all(),
        'revisions': doc_request.revisions.all(),
    })


# ============================================================
# TEAM: ADMIN DASHBOARD FOR DOCUMENT REQUESTS
# ============================================================
@login_required
def admin_doc_requests_view(request):
    """Team dashboard for managing all document requests"""
    
    if getattr(request.user, 'role', '') not in ['admin', 'agent'] and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard')
    
    status_filter = request.GET.get('status', '')
    requests_qs = DocumentCreationRequest.objects.select_related('client', 'assigned_team_member')
    
    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)
    
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        action = request.POST.get('action')
        doc_request = get_object_or_404(DocumentCreationRequest, id=request_id)
        
        if action == 'assign_to_me':
            doc_request.assigned_team_member = request.user
            doc_request.status = 'research'
            doc_request.save()
            messages.success(request, f'Assigned to you.')
        
        elif action == 'reject':
            doc_request.status = 'rejected'
            doc_request.save()
            messages.info(request, 'Request rejected.')
        
        return redirect('admin_doc_requests')
    
    return render(request, 'orders/admin_doc_requests.html', {
        'requests': requests_qs,
        'status_choices': DocumentCreationRequest.STATUS_CHOICES,
        'current_filter': status_filter,
    })


# ============================================================
# CLIENT: MY DOCUMENT REQUESTS
# ============================================================
@login_required
def my_doc_requests_view(request):
    """Client views all their document requests"""
    
    requests_qs = DocumentCreationRequest.objects.filter(client=request.user).order_by('-created_at')
    return render(request, 'orders/my_doc_requests.html', {
        'requests': requests_qs,
    })
