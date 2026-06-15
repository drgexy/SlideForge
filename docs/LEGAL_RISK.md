# Legal Risk Notes

This is not legal advice. Ask a qualified lawyer before commercial release or wide distribution.

## Lower-Risk Choices

- Do not include proprietary vendor SDKs, DLLs, `.so` files, or copied code from commercial SDPC viewers.
- Do not include patient data or real slide samples.
- Publish only your own source code and small synthetic test fixtures.
- Clearly document that SDPC support is based on observed files and may not support every vendor variant.

## Main Risk Areas

### Proprietary Format / Reverse Engineering

The converter parses SDPC byte structures directly. Reverse engineering for interoperability can be lawful in some jurisdictions and restricted in others, depending on contracts, local law, and how information was obtained.

Avoid shipping any vendor code or bypassing access-control mechanisms.

### HEVC / H.265 Patents

SDPC tiles in the tested files are HEVC/H.265 streams. HEVC can involve patent licensing obligations in some jurisdictions and use cases, especially for distributing products that include decoders.

To reduce distribution risk, this project should depend on user-installed PyAV/FFmpeg rather than bundling FFmpeg binaries inside the repository or app package.

### FFmpeg / PyAV Licensing

PyAV is BSD-3-Clause licensed, but it binds to FFmpeg libraries. FFmpeg may be LGPL or GPL depending on build configuration and enabled components. If you bundle FFmpeg libraries, you must comply with their license terms.

The GitHub repository should not vendor FFmpeg binaries unless you have reviewed the exact build license obligations.

### Clinical / Medical Risk

This is a research conversion tool, not a medical device. Do not market it for clinical diagnosis without regulatory review.

## Suggested GitHub Disclaimer

> SlideForge Mac is a research tool for converting supported SDPC whole-slide image files into OpenSlide-readable WSI files. It is not intended for clinical diagnosis. The project does not include proprietary SDPC vendor libraries, FFmpeg binaries, or patient data. Users are responsible for ensuring they have rights to process their slide files and for complying with codec, patent, and software license obligations in their jurisdiction.
