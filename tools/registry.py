"""Map public tool names from agent outputs to Python callables."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

from tools.tools import (
    get_balance_sheet_info,
    get_cashflow_info,
    get_income_statement_info,
    get_note_info,
    get_related_info,
    web_search,
)

TOOLS_MAPPING_2_FUNCTIONS = {
    "get_related_info": get_related_info,
    "get_balance_sheet_info": get_balance_sheet_info,
    "get_income_statement_info": get_income_statement_info,
    "get_cashflow_info": get_cashflow_info,
    "get_note_info": get_note_info,
    "web_search": web_search,
}
