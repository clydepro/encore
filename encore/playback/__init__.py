"""mpv control: JSON IPC client, state machine, supervisor and recovery.

The Playback Service is the only component that controls the audio engine
(SAPRS 7.1). Playback code never imports HTTP or presentation code and never
embeds hardware-specific assumptions (SAPRS 7.7, 7.9).
"""
