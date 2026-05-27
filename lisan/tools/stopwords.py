"""Shared stopword sets used by entity and noun-phrase extraction heuristics.

Centralized here so the writer's fallback regex, the elicitor's fallback regex,
and the post-fanout entity validator all agree on what is *not* a person.

The list intentionally excludes month names by default: users named after months
(August, May, June) need to remain valid people. Months are caught only by the
*combined* check `name in SENTENCE_STOPWORDS and name not in primer_known_names()`
which lives in the writer / fanout validator, never by this module alone.
"""

from __future__ import annotations


# Common sentence-initial pronouns, conjunctions, and articles.
SENTENCE_INITIAL_STOPWORDS: frozenset[str] = frozenset({
    "I", "My", "Me", "Mine",
    "The", "A", "An",
    "It", "He", "She", "They", "We", "You", "His", "Her", "Their", "Our",
    "No", "Yes", "Ok", "Okay", "So", "But", "And", "Or",
    "In", "On", "At", "Of", "For", "With", "From", "By", "Up", "Out",
})

# Interrogatives and common sentence-starting adverbs.
INTERROGATIVE_STOPWORDS: frozenset[str] = frozenset({
    "What", "Why", "How", "When", "Where", "Who", "Whom", "Whose", "Which",
    "Then", "Now", "Today", "Tomorrow", "Yesterday",
    "Strategically", "Honestly", "Frankly", "Maybe", "Perhaps", "Probably",
    "Anyway", "Actually", "Eventually", "Finally", "Basically", "Apparently",
    "Hopefully", "Obviously", "Clearly", "Suddenly", "Recently",
})

# Days of the week — never person names.
DAY_STOPWORDS: frozenset[str] = frozenset({
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
})

# Month names — names like August, May, June, April are legitimate human names,
# so this set is queried *only* in combination with the primer-known-names
# allowlist. Callers that need to filter sentence-initial capitalized tokens
# should compose: token in MONTH_STOPWORDS and token not in primer cast.
MONTH_STOPWORDS: frozenset[str] = frozenset({
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
})

# Common tools, platforms, and brand-name capitalized terms that are not people.
TOOL_STOPWORDS: frozenset[str] = frozenset({
    "Slack", "Zoom", "GitHub", "Gmail", "Notion", "Jira", "Linear",
    "Google", "Microsoft", "Apple", "Discord", "Figma", "Trello",
    "Asana", "Confluence", "Outlook", "Teams", "Dropbox", "OneDrive",
    "Excel", "Word", "PowerPoint", "Sheets", "Docs", "Calendar",
    "YouTube", "Twitter", "Reddit", "Facebook", "Instagram",
    "ChatGPT", "Claude", "OpenAI", "Anthropic",
})


# Union for callers that want a single set of "definitely not a person" terms.
# Months are deliberately excluded; query MONTH_STOPWORDS separately in
# combination with the primer allowlist when needed.
SENTENCE_INITIAL_OR_TOOL_STOPWORDS: frozenset[str] = (
    SENTENCE_INITIAL_STOPWORDS
    | INTERROGATIVE_STOPWORDS
    | DAY_STOPWORDS
    | TOOL_STOPWORDS
)
