import ctypes
from ctypes import c_uint, byref, c_void_p

winmm = ctypes.windll.winmm

# Get current volume
vol = c_uint()
res = winmm.waveOutGetVolume(0, byref(vol))
print(f"get_result={res} raw={vol.value} hex={vol.value:#010x}")
left = vol.value & 0xFFFF
right = (vol.value >> 16) & 0xFFFF
print(f"left={left} ({left/655.35:.1f}%) right={right} ({right/655.35:.1f}%)")
