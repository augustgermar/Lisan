# youtube_transcript

Fetch a YouTube video's transcript/captions as plain text (optionally with
timestamps) for summarizing or answering questions about a video.

Two fetch paths, in order:

1. `youtube-transcript-api`, if that package happens to be installed.
2. A standard-library fallback that talks to YouTube's innertube player
   endpoint directly (the same surface the Android client uses). Human
   captions are preferred over auto-generated (`asr`) tracks; the requested
   `language` wins over both.

The innertube surface is unofficial and can change — failures degrade to a
readable error message, never a stack trace. Long transcripts are truncated
at `max_chars` (default 12000) to keep tool results context-friendly.

No credentials or API key needed.
