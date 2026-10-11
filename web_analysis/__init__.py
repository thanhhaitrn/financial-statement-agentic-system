"""Company-news analysis for the open report (WEB_ANALYSIS_ENABLED).

``context`` decides what to look up, ``pipeline`` analyses what the evidence
stage fetched, ``render`` writes the answer section. The graph node lives in
``graph.web_analysis_node``.
"""

from web_analysis.context import build_web_context
from web_analysis.pipeline import analyze_company_news, articles_from_evidence
from web_analysis.render import render_news_section

__all__ = ["analyze_company_news", "articles_from_evidence", "build_web_context", "render_news_section"]
