import ctypes
from ctypes import c_uint, byref

winmm = ctypes.windll.winmm

# 40 percent: 0.40 * 65535 = 26214 = 0x6666
level = int(round(65535 * 0.40))
print(f"target_level={level} hex={level:#06x}")
dw = (level << 16) | level
print(f"dwVolume={dw} hex={dw:#010x}")

res = winmm.waveOutSetVolume(0, dw)
print(f"set_result={res} (0=MMSYSERR_NOERROR)")

# Read back
vol = c_uint()
res2 = winmm.waveOutGetVolume(0, byref(vol))
left = vol.value & 0xFFFF
right = (vol.value >> 16) & 0xFFFF
print(f"verify_raw={vol.value} hex={vol.value:#010x}")
print(f"left={left} ({left/655.35:.1f}%) right={right} ({right/655.35:.1f}%)")
