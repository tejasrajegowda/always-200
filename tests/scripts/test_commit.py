from scripts.commit import message_problem


def test_a_short_one_line_message_is_accepted():
    assert message_problem("Set up the repo") is None


def test_empty_multi_line_and_long_messages_are_refused():
    assert message_problem("")
    assert message_problem("Subject\n\nBody")
    assert message_problem("x" * 101)
    assert message_problem("x" * 100) is None
