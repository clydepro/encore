# User Guide

Status: **outline** — Encore has no runnable interface yet. Written for the
person holding a phone at a party, and for whoever set that phone on the table.

## What Encore is

A jukebox on your own network. One box plays your music; everyone in the room
adds to a shared queue from their own device. No accounts, no app store, no
cloud.

## Requirements

- Encore installed and running (see [Administrator guide](Administrator-Guide.md)).
- A device on the same local network.
- A browser.

## Joining the party

1. Scan the QR code on the box, or open `http://jukebox.home.arpa`.
2. The current track appears. That is the whole setup.

## Finding music

- **Search** — artist, album or song. Partial words work; typing an artist's
  first name is enough.
- **Browse artists** — alphabetical; pick one to see their albums and songs.
- **Browse albums** — cover art and track listings.

## Queueing

- Tap a song. It joins the end of the queue and appears in **Up Next**.
- Duplicates are allowed: if the song should play twice, queue it twice.
- The queue is strictly first-in, first-out. Nothing jumps the line, including
  the host — control comes from the appliance, not from privilege.
- There is no "who queued this". That is deliberate: anonymity is part of the
  game (ADR-007).

## Now Playing

- Cover art, title, artist, album and progress.
- Updates arrive by itself; never refresh, and the page works when you walk
  out of range and back.

## Troubleshooting on a phone

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| Page will not load | Wrong network (guest VLAN, VPN) | Join the LAN the box is on |
| No artwork | Cache on a slow link | Reload; check the artwork dir on the box |
| Queue did not move | Network blip | Tap again — duplicates are allowed |
| Silence | Audio output | Check the box, see [Troubleshooting](Troubleshooting.md) |

## Etiquette the design relies on

- Queue what you want to hear, not everything at once: `queue.max_items` bounds
  the line.
- Do not re-queue the same song forty times; it is allowed and unfunny.
- The host can skip, pause and stop. Everyone else can only add.

## Future

Ratings, favourites, "recently played" and suggestions are planned post-1.0
(SAPRS 16.3). They will be designed to preserve guest anonymity.
