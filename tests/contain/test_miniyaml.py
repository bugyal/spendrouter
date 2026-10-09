import pytest

from spendrouter.miniyaml import YAMLError, loads

DOC = """
# top comment
db_path: ~/.local/share/spendrouter/ledger.sqlite3   # trailing comment; "~" alone is null, "~/x" is not
timezone: "utc"
proxy:
  listen: 127.0.0.1:8787
  require_credential: false
  upstream_timeout: 600
upstreams:
  openai:
    base_url: https://api.openai.com
budgets:
  - scope: customer
    match: "*"
    soft_usd: 5
    hard_usd: 1_500
  - scope: global
    hard_usd: 2.5
    action: pause
hooks:
  - events: [breaker_tripped, "hard_cap_exceeded"]
    command: ["/bin/notify", "--to", 'ops # not a comment']
pricing:
  my-model: {input: 3.0, output: 12}
empty:
note: don't panic # apostrophes in plain text are not quotes
url_list:
- http://a.example/x
- 'it''s quoted'
"""


def test_parses_the_config_subset():
    data = loads(DOC)
    assert data["db_path"] == "~/.local/share/spendrouter/ledger.sqlite3"
    assert data["timezone"] == "utc"
    assert data["proxy"] == {"listen": "127.0.0.1:8787", "require_credential": False, "upstream_timeout": 600}
    assert data["upstreams"]["openai"]["base_url"] == "https://api.openai.com"
    assert data["budgets"] == [
        {"scope": "customer", "match": "*", "soft_usd": 5, "hard_usd": 1500},
        {"scope": "global", "hard_usd": 2.5, "action": "pause"},
    ]
    assert data["hooks"][0]["events"] == ["breaker_tripped", "hard_cap_exceeded"]
    assert data["hooks"][0]["command"] == ["/bin/notify", "--to", "ops # not a comment"]
    assert data["pricing"]["my-model"] == {"input": 3.0, "output": 12}
    assert data["empty"] is None
    assert data["note"] == "don't panic"
    assert data["url_list"] == ["http://a.example/x", "it's quoted"]


def test_agrees_with_pyyaml_on_doc():
    # The shipped template gets the same cross-check in test_unified_config.
    yaml = pytest.importorskip("yaml")
    assert loads(DOC) == yaml.safe_load(DOC)


def test_nested_sequences_and_scalars():
    data = loads("a:\n  - - 1\n    - 2\n  - x: ~\n    y: true\n  -\n    z: -3\n")
    assert data == {"a": [[1, 2], {"x": None, "y": True}, {"z": -3}]}


def test_empty_document():
    assert loads("") is None
    assert loads("# only a comment\n\n") is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("a:\n\tb: 1\n", "tabs"),
        ("a: &x 1\n", "anchors"),
        ("match: *\n", "quote it"),
        ("a: |\n  text\n", "block scalars"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("a: 1\n  b: 2\n", "indentation"),
        ("a: 'open\n", "unterminated"),
        ("a: [1, 2\n", "unterminated"),
        ("just a line\n", "key: value"),
        ("a: 1\n---\nb: 2\n", "multiple documents"),
    ],
)
def test_rejects_what_it_does_not_support(text, message):
    with pytest.raises(YAMLError) as info:
        loads(text)
    assert message in str(info.value)
    assert "line" in str(info.value)
