# orders/services/ai_document_service.py

import requests
import json
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class FreeAIDocumentService:
    """
    Uses FREE AI APIs:
    - Groq (primary): Ultra-fast, generous free tier
    - Google Gemini (backup): Huge context window
    """
    
    def __init__(self):
        # Get these from environment variables (free to sign up)
        self.groq_api_key = getattr(settings, 'GROQ_API_KEY', '')
        self.gemini_api_key = getattr(settings, 'GEMINI_API_KEY', '')
        
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self.gemini_api_key}"
    
    def generate_document_draft(self, doc_request):
        """
        Generate initial draft using research notes + client instructions
        """
        
        # Build comprehensive prompt
        prompt = self._build_generation_prompt(doc_request)
        
        # Try Groq first (faster, better free tier)
        try:
            if self.groq_api_key:
                return self._call_groq(prompt, doc_request)
        except Exception as e:
            logger.warning(f"Groq failed: {e}, trying Gemini...")
        
        # Fallback to Gemini
        try:
            if self.gemini_api_key:
                return self._call_gemini(prompt, doc_request)
        except Exception as e:
            logger.error(f"Gemini also failed: {e}")
            raise Exception("Both AI services failed. Check API keys.")
    
    def format_to_latex(self, draft_text, document_type):
        """
        Convert draft to properly formatted LaTeX
        """
        
        prompt = f"""You are a LaTeX expert. Convert the following {document_type} to professional LaTeX code.

REQUIREMENTS:
- Use appropriate document class (article for essays, report for long documents)
- Include proper packages (geometry, hyperref, graphicx, etc.)
- Use proper sectioning (\\section, \\subsection, etc.)
- Format citations properly (use BibTeX if references provided)
- Include title page if appropriate
- Use proper margins and font sizes
- Make it look professional and academic

DOCUMENT TYPE: {document_type}

DRAFT TO CONVERT:
{draft_text}

IMPORTANT: Return ONLY the complete LaTeX code, starting with \\documentclass and ending with \\end{{document}}. No explanations, just the code."""

        try:
            if self.groq_api_key:
                result = self._call_groq(prompt, None, max_tokens=8000)
                return self._extract_latex(result)
        except Exception:
            pass
        
        # Fallback
        if self.gemini_api_key:
            result = self._call_gemini(prompt, None)
            return self._extract_latex(result)
        
        raise Exception("LaTeX formatting failed")
    
    def _build_generation_prompt(self, doc_request):
        """Build comprehensive prompt with all context"""
        
        prompt = f"""You are an expert academic writer. Create a high-quality {doc_request.get_document_type_display()} based on the following information.

═══════════════════════════════════════
CLIENT REQUIREMENTS
═══════════════════════════════════════

TITLE: {doc_request.title}

DESCRIPTION:
{doc_request.description}

SPECIFIC INSTRUCTIONS:
{doc_request.instructions}

TARGET WORD COUNT: {doc_request.word_count_target} words

═══════════════════════════════════════
RESEARCH NOTES (from NotebookLM)
═══════════════════════════════════════

{doc_request.research_notes or 'No research notes provided yet.'}

═══════════════════════════════════════
WRITING GUIDELINES
═══════════════════════════════════════

1. Write in clear, professional academic English
2. Structure the document logically with proper sections
3. Include citations where appropriate (use [1], [2] format)
4. Ensure proper flow and coherence between sections
5. Meet the target word count (±10%)
6. Use appropriate academic tone for the document type
7. Include an introduction and conclusion
8. Add references section if sources were provided

QUALITY STANDARDS:
- No plagiarism
- Factually accurate (based on research notes)
- Well-organized with clear headings
- Professional formatting
- Error-free grammar and spelling

Please generate the complete document now."""

        return prompt
    
    def _call_groq(self, prompt, doc_request=None, max_tokens=4000):
        """Call Groq API (fastest free option)"""
        
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "llama-3.3-70b-versatile",  # Best free model on Groq
            "messages": [
                {
                    "role": "system",
                    "content": "You are a professional academic writer with expertise in research, technical writing, and document formatting."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "top_p": 0.9
        }
        
        response = requests.post(self.groq_url, headers=headers, json=data, timeout=120)
        response.raise_for_status()
        
        result = response.json()
        content = result['choices'][0]['message']['content']
        
        # Track usage
        if doc_request:
            doc_request.ai_model_used = 'groq-llama-3.3-70b'
            doc_request.ai_draft_tokens = result.get('usage', {}).get('total_tokens', 0)
        
        return content
    
    def _call_gemini(self, prompt, doc_request=None):
        """Call Google Gemini API (backup with huge context)"""
        
        data = {
            "contents": [{
                "parts": [{
                    "text": prompt
                }]
            }],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 8000,
                "topP": 0.9
            }
        }
        
        response = requests.post(self.gemini_url, json=data, timeout=120)
        response.raise_for_status()
        
        result = response.json()
        content = result['candidates'][0]['content']['parts'][0]['text']
        
        if doc_request:
            doc_request.ai_model_used = 'gemini-1.5-flash'
        
        return content
    
    def _extract_latex(self, text):
        """Extract LaTeX code from AI response (sometimes includes markdown)"""
        
        # Remove markdown code blocks if present
        if '```latex' in text:
            text = text.split('```latex')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]
        
        return text.strip()
