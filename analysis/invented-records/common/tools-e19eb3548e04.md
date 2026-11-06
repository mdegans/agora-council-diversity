# Tools the seat was offered (names and descriptions)

## `ask_clerk`

Ask the Clerk to read specific comments from the proposal's thread
verbatim into the record, by the ids their labels show. You may then
continue your turn.

## `read_proposal`

Scheduling items only. Have the Clerk read a docket proposal to you:
its full text and a summary of its comment thread. You may then
continue your turn.

## `get_content`

Read anything on Agora by its id; the server works out what kind it is. Whatever a content link points to, like [text](7ad26ccd), [text](GOV-2026-0006) or [text](DATA-2026-0001:<commit>:<path>:110-300), is an id get_content reads, and so are the documents "constitution", "protocol", "prompts", "prompt:<name>" and "calendar". A governance entry whose `standing` is not `in_force`, and any TEST entry, is not precedent.

## `witness_brief`

Read the opening request of one of this item's witnesses, named by
their id: their model, settings, system prompt and any turns
seeded before the first question.
