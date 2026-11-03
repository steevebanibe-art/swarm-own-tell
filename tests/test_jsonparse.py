from types import SimpleNamespace

from harness.jsonparse import balanced_objects, parse_reply, text_of


def blocks(*texts):
    return [{"type": "text", "text": t} for t in texts]


def test_strict_object():
    r = parse_reply(blocks('  {"tool": "wait", "args": {}}\n'), "end_turn")
    assert r.strict and r.lenient and r.reason is None
    assert r.obj == {"tool": "wait", "args": {}}


def test_code_fence_is_lenient_only():
    r = parse_reply(blocks('```json\n{"tool": "solve", "args": {"answer": "X"}}\n```'), "end_turn")
    assert not r.strict and r.lenient
    assert r.obj["args"]["answer"] == "X"


def test_prose_around_object_and_braces_inside_strings():
    text = 'Sure. I will do this: {"tool": "cache_put", "args": {"key": "a/b", "value": "x {not} y \\" }"}} Done.'
    r = parse_reply(blocks(text), "end_turn")
    assert not r.strict and r.lenient
    assert r.obj["args"]["value"] == 'x {not} y " }'


def test_first_parsable_candidate_wins():
    r = parse_reply(blocks("{oops} then {\"tool\": \"wait\", \"args\": {}} and {\"tool\": \"solve\"}"), "end_turn")
    assert r.lenient and r.obj["tool"] == "wait"


def test_unbalanced_prefix_then_object():
    assert list(balanced_objects('{ "a": 1 ... {"b": 2}')) == ['{"b": 2}']


def test_max_tokens_and_refusal_are_invalid_even_with_json():
    good = blocks('{"tool": "wait", "args": {}}')
    for reason in ("max_tokens", "refusal"):
        r = parse_reply(good, reason)
        assert not r.strict and not r.lenient and r.reason == reason and r.obj is None


def test_no_text_and_no_object():
    assert parse_reply([], "end_turn").reason == "no_text"
    assert parse_reply(blocks("I would rather not."), "end_turn").reason == "no_object"
    assert parse_reply(blocks("[1, 2]"), "end_turn").reason == "no_object"  # a list is not an object


def test_only_text_blocks_are_read():
    content = [
        {"type": "thinking", "thinking": '{"tool": "solve"}'},
        SimpleNamespace(type="text", text='{"tool": '),
        {"type": "text", "text": '"wait", "args": {}}'},
        {"type": "tool_use", "input": {"x": 1}},
    ]
    assert text_of(content) == '{"tool": "wait", "args": {}}'
    r = parse_reply(content, "end_turn")
    assert r.strict and r.obj["tool"] == "wait"


def test_to_record_logs_both_flags():
    rec = parse_reply(blocks('x {"a": 1}'), "end_turn").to_record()
    assert rec == {"strict_valid": False, "lenient_valid": True, "invalid_reason": None}
