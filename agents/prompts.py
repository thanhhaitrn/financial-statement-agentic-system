from langchain_core.prompts import ChatPromptTemplate

PROMPT_TEMPLATE = ChatPromptTemplate.from_messages([
    ("system", "You are {role}"),
    ("system", "You can access to these actions:\n{tools_list}"),
    ("human", "User query: {query}"),
    ("system", "Original user query:\n{user_query}"),
    ("system", "Planner plan (JSON):\n{plan_json}"),
    ("system", "Worker results (JSON):\n{worker_results_json}"),
    ("system", "Web summary:\n{web_summary}"),
    ("system", "Previous agent response:\n{last_agent_response}"),
    ("system", "Past tool observations:\n{tool_observations}"),
    ("system", "{system_instruction}")
])