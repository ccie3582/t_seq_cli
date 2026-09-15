// seqd.scd - bridge between seq.py and SuperCollider.
//
// Run this file once in SuperCollider, *after* booting the audio server:
//
//     s.boot;                  // then evaluate this file, or from sclang:
//     File("C:/CLI_1/sc/seqd.scd".standardizePath).load;
//
// It installs the OSC responders the sequencer talks to. After that,
// "synthdef <name>" / "synthdef all" in the CLI write synthdefs/*.scd and ask
// sclang here to compile and add them to the server.
//
// If you play many notes at once, boot the server with more memory and a
// smaller block size first (FoxDot does the same), e.g.:
//
//     s.options.memSize = 131072;      // 128 MB instead of 8 MB
//     s.options.blockSize = 128;       // finer scheduling
//     s.boot;

(
var port = NetAddr.langPort;

OSCdef(\seqd_load, { |msg|
	var path = msg[1].asString;
	("[seqd] loading SynthDef from " ++ path).postln;
	try {
		(path.standardizePath.load)
	} { |err|
		("[seqd] error loading " ++ path ++ ": " ++ err.errorString).postln
	};
}, "/seqd");

// Optional: free everything (the CLI's 'panic' frees the server groups
// directly, this is here for symmetry / manual use from SuperCollider).
OSCdef(\seqd_free, { |msg|
	"[seqd] freeing all nodes".postln;
	s.freeAll;
}, "/seqd_free");

("[seqd] bridge ready on port " ++ port ++ " (synthdefs via /seqd)").postln;
)
