# orders/views/document_creation_views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from .models import DocumentCreationRequest, DocumentSourceFile, DocumentRevision
from .services.ai_document_service import FreeAIDocumentService

@login_required
def create_document_request_view(request):
    """Client submits a new document creation request"""
    
    if request.method == 'POST':
        try:
            with transaction.atomic():
                # Create the request
                doc_request = DocumentCreationRequest.objects.create(
                    client=request.user,
                    document_type=request.POST.get('document_type'),
                    title=request.POST.get('title'),
                    description=request.POST.get('description'),
                    instructions=request.POST.get('instructions'),
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
                    f'✅ Your document request "{doc_request.title}" has been submitted! '
                    f'Our team will review it and start working soon.'
                )
                
                return redirect('doc_request_detail', request_id=doc_request.id)
                
        except Exception as e:
            messages.error(request, f'Error creating request: {str(e)}')
    
    return render(request, 'orders/create_document_request.html', {
        'document_types': DocumentCreationRequest.DOCUMENT_TYPES,
    })


@login_required
def doc_request_detail_view(request, request_id):
    """View document request details (client or team member)"""
    
    doc_request = get_object_or_404(DocumentCreationRequest, id=request_id)
    
    # Check permissions
    is_client = (doc_request.client == request.user)
    is_team = (request.user.role in ['admin', 'agent'] or request.user.is_staff)
    
    if not (is_client or is_team):
        messages.error(request, 'You do not have permission to view this request.')
        return redirect('dashboard')
    
    # Handle client revision request
    if request.method == 'POST' and is_client and doc_request.can_request_revision():
        revision_notes = request.POST.get('revision_notes', '').strip()
        if revision_notes:
            DocumentRevision.objects.create(
                request=doc_request,
                client_notes=revision_notes
            )
            doc_request.revision_count += 1
            doc_request.status = 'revision'
            doc_request.save()
            
            messages.success(request, '✅ Your revision request has been submitted!')
            return redirect('doc_request_detail', request_id=doc_request.id)
    
    # Handle client final approval
    if request.method == 'POST' and is_client and doc_request.status == 'client_review':
        if 'approve' in request.POST:
            doc_request.status = 'approved'
            doc_request.save()
            messages.success(request, '✅ Document approved! It will now be prepared for printing.')
            return redirect('doc_request_detail', request_id=doc_request.id)
    
    return render(request, 'orders/doc_request_detail.html', {
        'doc_request': doc_request,
        'is_client': is_client,
        'is_team': is_team,
        'source_files': doc_request.source_files.all(),
        'revisions': doc_request.revisions.all(),
    })


@login_required
def admin_doc_requests_view(request):
    """Team dashboard for managing all document requests"""
    
    if request.user.role not in ['admin', 'agent'] and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard')
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    requests = DocumentCreationRequest.objects.select_related('client', 'assigned_team_member')
    
    if status_filter:
        requests = requests.filter(status=status_filter)
    
    # Handle team actions
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        action = request.POST.get('action')
        doc_request = get_object_or_404(DocumentCreationRequest, id=request_id)
        
        if action == 'assign_to_me':
            doc_request.assigned_team_member = request.user
            doc_request.status = 'research'
            doc_request.save()
            messages.success(request, f'Assigned "{doc_request.title}" to yourself.')
        
        elif action == 'start_research':
            doc_request.status = 'research'
            doc_request.save()
            messages.info(request, 'Upload sources to NotebookLM and paste research notes.')
        
        elif action == 'generate_draft':
            try:
                ai_service = FreeAIDocumentService()
                draft = ai_service.generate_document_draft(doc_request)
                doc_request.ai_draft_text = draft
                doc_request.status = 'formatting'
                doc_request.save()
                messages.success(request, '✅ AI draft generated! Now formatting to LaTeX...')
            except Exception as e:
                messages.error(request, f'AI generation failed: {str(e)}')
        
        elif action == 'format_latex':
            try:
                ai_service = FreeAIDocumentService()
                latex = ai_service.format_to_latex(doc_request.ai_draft_text, doc_request.get_document_type_display())
                doc_request.latex_code = latex
                doc_request.status = 'human_review'
                doc_request.save()
                messages.success(request, '✅ LaTeX generated! Please review and create Overleaf project.')
            except Exception as e:
                messages.error(request, f'LaTeX formatting failed: {str(e)}')
        
        elif action == 'send_to_client':
            overleaf_url = request.POST.get('overleaf_url', '').strip()
            if overleaf_url:
                doc_request.overleaf_project_url = overleaf_url
            doc_request.status = 'client_review'
            doc_request.save()
            messages.success(request, '✅ Sent to client for review!')
        
        elif action == 'mark_printing':
            doc_request.status = 'printing'
            doc_request.save()
            messages.success(request, '✅ Marked as ready for printing!')
        
        return redirect('admin_doc_requests')
    
    return render(request, 'orders/admin_doc_requests.html', {
        'requests': requests,
        'status_choices': DocumentCreationRequest.STATUS_CHOICES,
        'current_filter': status_filter,
    })
