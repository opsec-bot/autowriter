#!/usr/bin/env python3
"""
AutoWriter - Types your pre-written text with realistic human-like pauses.

Usage:
  python autowriter.py
  Paste your text, press Enter twice, then switch to your target window.
  Press F9 to start typing. Press ESC to stop.

Requires: pip install pynput
"""

import sys
import time
import random
import re
import threading

try:
    from pynput import keyboard
    from pynput.keyboard import Controller, Key
except ImportError:
    print("Error: pynput is required.  pip install pynput")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

TRIGGER_KEY  = Key.f9     # press this to start typing (in any window)
STOP_KEY     = Key.esc    # press this to abort

WPM_MIN      = 52         # slowest typing speed
WPM_MAX      = 95         # fastest typing speed (during flow)

# Probability of entering a "locked-in" flow state after any pause
FLOW_CHANCE  = 0.22
FLOW_MIN_LEN = 2          # min sentences in a flow burst
FLOW_MAX_LEN = 7          # max sentences in a flow burst

# Pause windows (seconds) when NOT in flow state
PAUSE_QUICK  = (0.3,  1.3)   # 25% — next thought is ready
PAUSE_NORMAL = (1.3,  3.2)   # 45% — normal thinking
PAUSE_LONG   = (3.2,  5.0)   # 30% — hunting for the right word

# Pause window when locked in
PAUSE_FLOW   = (0.08, 0.85)


# ── Shared state ──────────────────────────────────────────────────────────────

typer        = Controller()
start_event  = threading.Event()
stop_event   = threading.Event()


# ── Keyboard listener ─────────────────────────────────────────────────────────

def on_press(key):
    if key == TRIGGER_KEY:
        start_event.set()
    elif key == STOP_KEY:
        stop_event.set()
        start_event.set()   # unblock .wait() if still waiting
        return False        # detach listener


# ── Text splitting ────────────────────────────────────────────────────────────

def split_into_chunks(text: str) -> list:
    """
    Returns a flat list of sentence strings interleaved with '\n' markers
    for paragraph breaks.  '\n' markers are handled by the typing loop
    as Enter keypresses rather than typed text.
    """
    paragraphs = re.split(r'\n{2,}', text.strip())
    chunks = []
    for para in paragraphs:
        # Split on sentence-ending punctuation followed by whitespace
        sentences = re.split(r'(?<=[.!?…])\s+', para.strip())
        chunks.extend(s.strip() for s in sentences if s.strip())
        chunks.append('\n')  # paragraph separator

    # Drop trailing paragraph markers
    while chunks and chunks[-1] == '\n':
        chunks.pop()

    return chunks


# ── Typing engine ─────────────────────────────────────────────────────────────

def type_chunk(text: str, wpm: float):
    """Type a string one character at a time with human-like jitter."""
    # chars per second (average word = 5 chars)
    cps        = (wpm * 5) / 60.0
    base_delay = 1.0 / cps

    for char in text:
        if stop_event.is_set():
            return

        if char == '\n':
            typer.press(Key.enter)
            typer.release(Key.enter)
        else:
            typer.type(char)

        # triangular distribution: most keystrokes near base speed,
        # occasional fast bursts and slow-downs
        delay = base_delay * random.triangular(0.35, 2.2, 1.0)

        # Very rare mid-word micro-hesitation
        if random.random() < 0.007:
            delay += random.uniform(0.08, 0.40)

        time.sleep(max(0.018, delay))


def _interruptible_sleep(duration: float):
    step    = 0.05
    elapsed = 0.0
    while elapsed < duration and not stop_event.is_set():
        time.sleep(min(step, duration - elapsed))
        elapsed += step


def pause_between(in_flow: bool) -> float:
    """Pick a pause length based on the current flow state."""
    if in_flow:
        return random.uniform(*PAUSE_FLOW)

    roll = random.random()
    if roll < 0.25:
        return random.uniform(*PAUSE_QUICK)
    elif roll < 0.70:
        return random.uniform(*PAUSE_NORMAL)
    else:
        return random.uniform(*PAUSE_LONG)


def autowrite(chunks: list):
    """
    Walk through sentence chunks, typing each one then pausing.
    Occasionally enters a 'flow state' where pauses collapse to almost nothing.
    """
    in_flow   = False
    flow_left = 0

    for i, chunk in enumerate(chunks):
        if stop_event.is_set():
            break

        # ── Paragraph break ──────────────────────────────────────────────────
        if chunk == '\n':
            typer.press(Key.enter)
            typer.release(Key.enter)
            typer.press(Key.enter)
            typer.release(Key.enter)
            _interruptible_sleep(random.uniform(0.3, 0.8))
            continue

        # ── Type this sentence ───────────────────────────────────────────────
        wpm_boost = 8 if in_flow else 0
        wpm = random.uniform(WPM_MIN + wpm_boost, WPM_MAX + wpm_boost)
        type_chunk(chunk, wpm)

        # ── Pause before next chunk ──────────────────────────────────────────
        if i < len(chunks) - 1 and not stop_event.is_set():
            next_chunk = chunks[i + 1]

            # Don't add a trailing space before a paragraph break
            if next_chunk != '\n':
                typer.type(' ')

            # Flow state bookkeeping
            if in_flow:
                flow_left -= 1
                if flow_left <= 0:
                    in_flow = False
            elif random.random() < FLOW_CHANCE:
                in_flow   = True
                flow_left = random.randint(FLOW_MIN_LEN, FLOW_MAX_LEN)

            _interruptible_sleep(pause_between(in_flow))


# ── Input collection ──────────────────────────────────────────────────────────

def collect_text() -> str:
    """Read pasted text from stdin.  Two consecutive blank lines = done."""
    w = 60
    print()
    print("  " + "─" * w)
    print("  AUTOWRITER")
    print("  " + "─" * w)
    print("  Paste your text.  Press ENTER twice when done.")
    print("  " + "─" * w)
    print()

    lines        = []
    blank_streak = 0

    try:
        while True:
            line = input()
            if line == '':
                blank_streak += 1
                if blank_streak >= 2:
                    break
                lines.append('')
            else:
                blank_streak = 0
                lines.append(line)
    except (EOFError, KeyboardInterrupt):
        pass

    while lines and lines[-1] == '':
        lines.pop()

    return '\n'.join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    text = collect_text()

    if not text.strip():
        print("\n  Nothing to type. Bye.")
        return

    chunks      = split_into_chunks(text)
    real_chunks = [c for c in chunks if c != '\n']

    print(f"\n  {len(real_chunks)} sentence(s) loaded.")
    print( "  Switch to your target window, then press F9 to start.")
    print( "  ESC stops at any time.\n")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    start_event.wait()

    if stop_event.is_set():
        print("  Cancelled.")
        listener.stop()
        return

    # Brief buffer so F9 key-up doesn't land in the target window
    time.sleep(0.28)

    autowrite(chunks)

    listener.stop()

    outcome = "Done!" if not stop_event.is_set() else "Stopped."
    print(f"\n  {outcome}")


if __name__ == '__main__':
    main()
