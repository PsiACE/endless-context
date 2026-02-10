from poweragent.agent import SimpleAgent


class FakeLLM:
    def __init__(self) -> None:
        self.messages = None

    def generate_response(self, messages, **_kwargs):
        self.messages = messages
        return "ok"


class FakeMemory:
    def __init__(self, results=None) -> None:
        self.llm = FakeLLM()
        self._results = results or {"results": []}
        self.add_calls = []

    def search(self, **_kwargs):
        return self._results

    def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))


def test_reply_includes_memory_context():
    memory = FakeMemory(results={"results": [{"memory": "Example memory"}]})
    agent = SimpleAgent(memory=memory, user_id="u1", agent_id="a1", memory_limit=3)
    reply = agent.reply("hello", history=[])

    assert reply == "ok"
    assert memory.add_calls
    assert any(msg["role"] == "system" and "Relevant memories" in msg["content"] for msg in memory.llm.messages)
