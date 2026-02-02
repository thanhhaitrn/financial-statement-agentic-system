from langchain_core.prompts import ChatPromptTemplate

PROMPT_TEMPLATE = ChatPromptTemplate.from_messages([
    ("system", "You are {role}"),
    ("system", "You can access to these actions:\n{tools_list}"),
    ("human", "User query: {query}"),
    ("system", "Previous agent response:\n{last_agent_response}"),
    ("system", "Past tool observations:\n{tool_observations}"),
    ("system", "{system_instruction}")
])