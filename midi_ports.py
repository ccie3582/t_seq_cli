"""MIDI port enumeration for Windows (winmm.dll via ctypes).

Shared by the CLI helper list-ports.py and the interactive seq shell
(select-port). No third-party MIDI dependency: python-rtmidi's cp314 wheel
is currently broken (heap corruption at import on CPython 3.14), so mido
cannot be used for enumeration until a fixed wheel is published.
"""

import ctypes
from ctypes import wintypes

MAXPNAMELEN = 32  # MAXPNAMELEN in the Windows SDK

# winmm wTechnology values for MIDI output devices.
_OUTPUT_TECHNOLOGIES = {
    1: "MIDI port",
    2: "synth",
    3: "square-wave synth",
    4: "FM synth",
    5: "MIDI mapper",
    6: "wavetable synth",
    7: "software synth",
}


class MIDIINCAPS(ctypes.Structure):
    """MIDIINCAPSW: leading fields up to the device name."""
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),  # MMVERSION
        ("szPname", ctypes.c_wchar * MAXPNAMELEN),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIOUTCAPS(ctypes.Structure):
    """MIDIOUTCAPSW: leading fields up to the device name and technology."""
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),  # MMVERSION
        ("szPname", ctypes.c_wchar * MAXPNAMELEN),
        ("wTechnology", wintypes.WORD),
        ("wVoiceMessages", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


def _bind(name, caps_type):
    """Bind a winmm caps function returning MMRESULT (UINT)."""
    func = ctypes.WinDLL("winmm").__getattr__(name)
    func.argtypes = [ctypes.c_size_t, ctypes.POINTER(caps_type), wintypes.UINT]  # UINT_PTR
    func.restype = wintypes.UINT
    return func


midi_in_get_num_devs = ctypes.WinDLL("winmm").midiInGetNumDevs
midi_in_get_num_devs.restype = wintypes.UINT
midi_out_get_num_devs = ctypes.WinDLL("winmm").midiOutGetNumDevs
midi_out_get_num_devs.restype = wintypes.UINT

midi_in_get_dev_caps = _bind("midiInGetDevCapsW", MIDIINCAPS)
midi_out_get_dev_caps = _bind("midiOutGetDevCapsW", MIDIOUTCAPS)


def _device_name(caps):
    """Extract the szPname string (str on some Pythons, array on others)."""
    pname = caps.szPname
    name = pname.strip() if isinstance(pname, str) else (pname.value or "")
    return name.strip()


def _port_names(count, get_caps, caps_type):
    """Return display names for MIDI device ids 0..count-1."""
    names = []
    for device_id in range(count):
        caps = caps_type()
        result = get_caps(device_id, ctypes.byref(caps), ctypes.sizeof(caps))
        if result != 0:  # MMSYSERR_NOERROR is 0
            names.append("(unavailable)")
        else:
            names.append(_device_name(caps))
    return names


def get_input_port_names():
    """Return the list of MIDI input port names."""
    return _port_names(midi_in_get_num_devs(), midi_in_get_dev_caps, MIDIINCAPS)


def get_output_port_names():
    """Return the list of MIDI output port names."""
    return _port_names(midi_out_get_num_devs(), midi_out_get_dev_caps, MIDIOUTCAPS)


def get_output_port_info():
    """Return output ports as (name, technology label) pairs."""
    info = []
    count = midi_out_get_num_devs()
    for device_id in range(count):
        caps = MIDIOUTCAPS()
        result = midi_out_get_dev_caps(device_id, ctypes.byref(caps), ctypes.sizeof(caps))
        if result != 0:
            info.append(("(unavailable)", ""))
        else:
            label = _OUTPUT_TECHNOLOGIES.get(caps.wTechnology, "")
            info.append((_device_name(caps), label))
    return info
