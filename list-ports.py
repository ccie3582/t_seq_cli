"""List MIDI input and output ports available on this Windows system.

Thin CLI over midi_ports.py (winmm.dll enumeration). Exit codes:
0 on success, 1 on unexpected error.
"""

import sys

import midi_ports


def main():
    try:
        inputs = midi_ports.get_input_port_names()
        outputs = midi_ports.get_output_port_names()
    except OSError as e:
        print(f"Error accessing the Windows MIDI API: {e}")
        return 1

    print("MIDI Input Ports:")
    if inputs:
        for i, name in enumerate(inputs, 1):
            print(f"  {i}: {name}")
    else:
        print("  (none found)")

    print("\nMIDI Output Ports:")
    if outputs:
        for i, name in enumerate(outputs, 1):
            print(f"  {i}: {name}")
    else:
        print("  (none found)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
