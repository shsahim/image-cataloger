from image_cataloger.parsing import clean_summary, parse_summaries, parse_tab_lines


def test_json_array():
    assert parse_summaries('["a cat", "a dog"]', 2) == ["a cat", "a dog"]


def test_json_array_in_fence_with_preamble():
    text = 'Here you go:\n```json\n["one", "two", "three"]\n```'
    assert parse_summaries(text, 3) == ["one", "two", "three"]


def test_json_objects():
    text = '[{"summary": "x"}, {"description": "y"}]'
    assert parse_summaries(text, 2) == ["x", "y"]


def test_count_mismatch_returns_none():
    assert parse_summaries('["only one"]', 2) is None
    assert parse_summaries('["a", "b", "c"]', 2) is None


def test_empty_entry_is_mismatch():
    assert parse_summaries('["a", ""]', 2) is None


def test_numbered_lines():
    text = "1. Photo of a beach\n2) Screenshot of a spreadsheet\n3: Scanned receipt reading 'TOTAL $4.20'"
    assert parse_summaries(text, 3) == [
        "Photo of a beach", "Screenshot of a spreadsheet", "Scanned receipt reading 'TOTAL $4.20'"]


def test_numbered_lines_out_of_order_or_missing():
    assert parse_summaries("1. a\n3. c", 3) is None


def test_single_bare_line():
    assert parse_summaries("A red bicycle leaning on a brick wall.", 1) == [
        "A red bicycle leaning on a brick wall."]
    assert parse_summaries("line one\nline two", 1) is None


def test_garbage():
    assert parse_summaries("", 2) is None
    assert parse_summaries("I cannot help with that.", 2) is None


def test_clean_summary():
    assert clean_summary('  "A   quoted\n line"  ') == "A quoted line"
    assert clean_summary("- bullet text") == "bullet text"


def test_tab_lines():
    text = "chatter\nIMG_1.jpg\tA dog on grass\n`IMG_2.jpg`\t  Beach at sunset \nnope"
    assert parse_tab_lines(text) == {"IMG_1.jpg": "A dog on grass", "IMG_2.jpg": "Beach at sunset"}
