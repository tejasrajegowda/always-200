You are writing a short incident note for the engineer on call.

Explain what the evidence below shows, using only the numbers given in its value column. Do not
invent, round or convert numbers, and do not quote minute labels as numbers.

Return only JSON that matches the schema you were given:
- `summary`: what changed, on which services, in plain words;
- `suspected_service`: the one service you suspect is the source of the problem;
- `evidence`: the table rows that support it (`ts` is the minute, e.g. -2 for t-2, and `value` is
  the number exactly as printed);
- `first_check`: one concrete first thing to check.
