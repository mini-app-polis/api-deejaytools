# ADR-0011: The season year rolls over on October 1

Date: 2026-10-25

## Status

Accepted

Records a rule in force since deejaytools-api (2026-08-24); written down
here because the year it produces surprises people.

## Context

Songs and events belong to a competitive season, and the season is named
by one year. The tour starts at The Open, on Thanksgiving weekend, and
routine submissions for it open in October. A song uploaded that late in
the calendar year is almost always meant for the next year's events.
Naming the season by both years ("2026-2027") is clumsy in filenames and
folders.

## Decision

The season year is the calendar year, plus one from October 1:

- January to September belong to that year's season; October to December
  belong to the next year's. A song uploaded in October 2026, and The Open
  held that November, are both season 2027.
- A song takes its season when its upload is accepted
  (`song_builds.season_year_now()`, the server's clock), and keeps it
  through retried builds.
- An event's season is its `season_year` override, else its start date's
  season (`domain.season_year_from_date_string`).
- The web app suggests an event's season the same way
  (deejaytools-com `src/lib/seasonYear.ts`); the rollover month must match
  everywhere it is defined.

## Consequences

- The season names the Drive year folder, is a segment of the processed
  filename, sets the year tag on the file, and scopes version numbering.
- Dancers who think in calendar years read a late-year upload as the wrong
  year. The web app does not show the season on songs or on a dancer's
  submissions; it remains in the Drive filename and folders, and the
  deejaytools-com help page ("Submitting music") explains it.
- Changing the rollover month moves songs uploaded around it into a
  different year folder and version sequence; it is a data change, not
  just a code change.
