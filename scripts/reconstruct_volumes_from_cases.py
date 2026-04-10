#!/usr/bin/env python3
"""Reconstruct 3D integer HU-like volumes from exported case slices.

Outputs per case:
  - volume_hu_int16.npy
  - volume_hu_int16.nii.gz
  - reconstruction_metadata.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct integer HU-like 3D volumes from case slice folders."
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path("cases"),
        help="Directory containing exported case folders.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/reconstructed_cases"),
        help="Directory where reconstructed case volumes will be written.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing reconstructed case outputs.",
    )
    return parser.parse_args()


def require_dependencies():
    try:
        import nibabel as nib  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Missing dependencies. Install `nibabel`, `numpy`, and `Pillow` before running this script."
        ) from exc
    return nib, np, Image


def load_case_metadata(case_dir: Path) -> dict:
    metadata_path = case_dir / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Missing metadata.json under {case_dir}")
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def list_case_dirs(cases_dir: Path) -> list[Path]:
    return sorted(path for path in cases_dir.iterdir() if path.is_dir())


def parse_instance_number(slice_path: Path) -> int:
    match = re.fullmatch(r"slice_(\d+)\.png", slice_path.name)
    if not match:
        raise ValueError(f"Unexpected slice filename format: {slice_path.name}")
    return int(match.group(1))


def build_affine(np_module, metadata: dict):
    pixel_spacing = metadata["PixelSpacing"]
    slice_thickness = float(metadata["SliceThickness"])
    orientation = metadata["ImageOrientationPatient"]
    origin = metadata["ImagePositionPatient"]

    row_cosines = np_module.asarray(orientation[:3], dtype=np_module.float32)
    col_cosines = np_module.asarray(orientation[3:], dtype=np_module.float32)
    slice_cosines = np_module.cross(row_cosines, col_cosines)

    affine = np_module.eye(4, dtype=np_module.float32)
    affine[:3, 0] = row_cosines * float(pixel_spacing[0])
    affine[:3, 1] = col_cosines * float(pixel_spacing[1])
    affine[:3, 2] = slice_cosines * slice_thickness
    affine[:3, 3] = np_module.asarray(origin, dtype=np_module.float32)
    return affine


def reconstruct_case(case_dir: Path, output_dir: Path, overwrite: bool) -> str:
    nib, np, Image = require_dependencies()

    metadata = load_case_metadata(case_dir)
    case_id = metadata["case_id"]
    slices_dir = case_dir / "slices"
    if not slices_dir.exists():
        raise FileNotFoundError(f"Missing slices directory under {case_dir}")

    output_case_dir = output_dir / case_id
    if output_case_dir.exists():
        if not overwrite:
            return f"skip {case_id}: output already exists"
        shutil.rmtree(output_case_dir)
    output_case_dir.mkdir(parents=True, exist_ok=True)

    raw_slice_paths = sorted(slices_dir.glob("slice_*.png"))
    indexed_slice_paths = sorted((parse_instance_number(path), path) for path in raw_slice_paths)

    expected_start = int(metadata["InstanceNumber"]["start"])
    expected_end = int(metadata["InstanceNumber"]["end"])
    expected_step = int(metadata["InstanceNumber"]["step"])
    expected_instances = list(range(expected_start, expected_end + 1, expected_step))
    found_instances = [instance_number for instance_number, _ in indexed_slice_paths]
    missing_instances = sorted(set(expected_instances) - set(found_instances))
    unexpected_instances = sorted(set(found_instances) - set(expected_instances))

    integrity_status = "complete"
    if missing_instances or unexpected_instances:
        integrity_status = "incomplete"

    slice_arrays = []
    for _, slice_path in indexed_slice_paths:
        if not slice_path.exists():
            raise FileNotFoundError(f"Missing slice file: {slice_path}")
        slice_image = Image.open(slice_path).convert("L")
        slice_arrays.append(np.asarray(slice_image, dtype=np.uint8))

    volume_uint8 = np.stack(slice_arrays, axis=-1)

    output_min, output_max = 0.0, 255.0
    window_center = float(metadata["WindowCenter"])
    window_width = float(metadata["WindowWidth"])
    window_min = float(window_center - window_width / 2.0)
    window_max = float(window_center + window_width / 2.0)
    volume_float = volume_uint8.astype(np.float32)
    volume_hu = window_min + ((volume_float - output_min) / (output_max - output_min)) * (
        window_max - window_min
    )
    volume_hu = np.rint(volume_hu).astype(np.int16)

    affine = build_affine(np, metadata)

    hu_npy_path = output_case_dir / "volume_hu_int16.npy"
    hu_nii_path = output_case_dir / "volume_hu_int16.nii.gz"

    np.save(hu_npy_path, volume_hu)
    nib.save(nib.Nifti1Image(volume_hu, affine), str(hu_nii_path))

    reconstruction_metadata = {
        "case_id": case_id,
        "source_case_dir": str(case_dir.resolve()),
        "source_slices_dir": str(slices_dir.resolve()),
        "num_slices": int(volume_uint8.shape[-1]),
        "expected_num_slices": len(expected_instances),
        "available_instance_numbers": found_instances,
        "missing_instance_numbers": missing_instances,
        "volume_shape_xyz": [int(v) for v in volume_uint8.shape],
        "ImagePositionPatient": metadata["ImagePositionPatient"],
        "ImageOrientationPatient": metadata["ImageOrientationPatient"],
        "PixelSpacing": metadata["PixelSpacing"],
        "Rows": metadata["Rows"],
        "Columns": metadata["Columns"],
        "SliceThickness": metadata["SliceThickness"],
        "WindowCenter": metadata["WindowCenter"],
        "WindowWidth": metadata["WindowWidth"],
        "reconstruction_mode": {
            "input_range": [output_min, output_max],
            "target_range": [window_min, window_max],
            "method": "linear_mapping_then_round_to_int16",
            "missing_slices_policy": "reconstruct_with_available_slices_only",
        },
        "outputs": {
            "volume_hu_int16_npy": str(hu_npy_path.resolve()),
            "volume_hu_int16_nii_gz": str(hu_nii_path.resolve()),
        },
        "value_ranges": {
            "input_uint8": [int(volume_uint8.min()), int(volume_uint8.max())],
            "volume_hu_int16": [int(volume_hu.min()), int(volume_hu.max())],
        },
    }
    (output_case_dir / "reconstruction_metadata.json").write_text(
        json.dumps(reconstruction_metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return (
        f"done {case_id}: reconstructed {len(found_instances)}/{len(expected_instances)} slices, "
        f"missing {len(missing_instances)}, hu_int16 range {int(volume_hu.min())}-{int(volume_hu.max())}"
    )


def main() -> None:
    args = parse_args()
    cases_dir = args.cases_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not cases_dir.exists():
        raise SystemExit(f"Cases directory not found: {cases_dir}")

    case_dirs = list_case_dirs(cases_dir)
    if not case_dirs:
        raise SystemExit(f"No case directories found under: {cases_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    results = [reconstruct_case(case_dir, output_dir, args.overwrite) for case_dir in case_dirs]
    for line in results:
        print(line)


if __name__ == "__main__":
    main()
