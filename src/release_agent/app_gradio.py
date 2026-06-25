"""
Gradio web chatbot for the Release Copilot.

Run:
    python src/release_agent/app_gradio.py

Exposes a nice chat UI. Works great for demos and can be the base for K8s deployment.
"""

import os
import uuid
from dotenv import load_dotenv
import gradio as gr

from langchain_core.messages import HumanMessage

from .agent import get_compiled_graph

load_dotenv()

# One graph instance for the app (in real multi-user you'd scope by session)
GRAPH = get_compiled_graph()


def chat_fn(message: str, history: list, thread_id: str, repo: str):
    if repo:
        os.environ["RELEASE_AGENT_TARGET_REPO"] = repo

    config = {"configurable": {"thread_id": thread_id or f"gradio-{uuid.uuid4().hex[:8]}"}}

    # Feed the user message
    input_state = {"messages": [HumanMessage(content=message)]}

    # Collect assistant text + any tool side effects
    assistant_text = ""
    last_state = None

    for chunk in GRAPH.stream(input_state, config=config, stream_mode="values"):
        last_state = chunk
        # Try to extract the latest assistant message content
        msgs = chunk.get("messages", [])
        if msgs:
            last = msgs[-1]
            c = getattr(last, "content", "")
            if c and not str(c).startswith("Tool") and not getattr(last, "tool_calls", None):
                assistant_text = str(c)

    # Handle pending interrupt (confirmation)
    snapshot = GRAPH.get_state(config)
    if snapshot.next and "gate" in snapshot.next:
        intr = snapshot.interrupts[0].value if snapshot.interrupts else {}
        token = intr.get("token", "CONFIRM-???")
        proposed = intr.get("proposed", {})
        prompt = (
            f"**Confirmation required**\n\n"
            f"Token: **`{token}`**\n\n"
            f"Proposed changes:\n```json\n{proposed}\n```\n\n"
            f"Reply with the token to continue (e.g. `{token}`)."
        )
        return history + [(message, prompt)], thread_id

    # Normal reply
    if not assistant_text and last_state:
        # fall back to showing last tool output or status
        tool_msgs = [m for m in last_state.get("messages", []) if getattr(m, "type", "") == "tool"]
        if tool_msgs:
            assistant_text = "Tool results:\n" + "\n".join(str(t.content)[:800] for t in tool_msgs[-2:])

    if not assistant_text:
        assistant_text = "Done. Check the tool output in the conversation for GitHub links."

    history = history + [(message, assistant_text)]
    return history, thread_id


def build_ui():
    with gr.Blocks(title="Release Copilot", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            "# 🚀 Release Copilot (LangGraph + gh)\n"
            "Talk to the agent to update image tags in JSON configs and trigger GitHub workflows.\n\n"
            f"**Target repo**: `{os.getenv('RELEASE_AGENT_TARGET_REPO', 'phaniuk111/devops')}`"
        )

        with gr.Row():
            thread_box = gr.Textbox(value=f"web-{uuid.uuid4().hex[:8]}", label="Thread ID (for continuity)", scale=1)
            repo_box = gr.Textbox(value=os.getenv("RELEASE_AGENT_TARGET_REPO", "phaniuk111/devops"), label="Target Repo", scale=1)

        chatbot = gr.Chatbot(height=520, label="Chat with Release Copilot")
        msg = gr.Textbox(placeholder="e.g. promote payments-api:2.0.33 and orders-api:v1.2.3", label="Your message")
        send = gr.Button("Send", variant="primary")

        def user_fn(user_message, history, tid, repo):
            return "", history + [(user_message, None)], tid, repo

        def bot_fn(history, tid, repo):
            user_msg = history[-1][0] if history else ""
            new_history, new_tid = chat_fn(user_msg, history[:-1] if history else [], tid, repo)
            # gradio chat expects list of tuples
            return new_history, new_tid

        msg.submit(user_fn, [msg, chatbot, thread_box, repo_box], [msg, chatbot, thread_box, repo_box], queue=False) \
           .then(bot_fn, [chatbot, thread_box, repo_box], [chatbot, thread_box])

        send.click(user_fn, [msg, chatbot, thread_box, repo_box], [msg, chatbot, thread_box, repo_box], queue=False) \
            .then(bot_fn, [chatbot, thread_box, repo_box], [chatbot, thread_box])

        gr.Examples(
            examples=[
                "list allowed images",
                "promote payments-api:2.0.33",
                "show status of recent runs",
            ],
            inputs=msg
        )

        gr.Markdown(
            "After the agent proposes, copy the `CONFIRM-XXXXXX` token it shows and paste it to continue.\n\n"
            "All actions are performed via your local GH token / the token you mount in K8s."
        )

    return demo


if __name__ == "__main__":
    demo = build_ui()
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False)
