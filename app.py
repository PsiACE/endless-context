import os

import gradio as gr

from poweragent.agent import SimpleAgent

_agent: SimpleAgent | None = None


def get_agent() -> SimpleAgent:
    global _agent
    if _agent is None:
        _agent = SimpleAgent()
    return _agent


def predict(message: str, history):
    try:
        return get_agent().reply(message, history)
    except Exception as exc:
        return f"Error: {exc}"


demo = gr.ChatInterface(
    predict,
    examples=[
        "Summarize the last messages in memory.",
        "What do you remember about my preferences?",
    ],
    chatbot=gr.Chatbot(
        height=600,
    ),
)

if __name__ == "__main__":
    demo.launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
    )
