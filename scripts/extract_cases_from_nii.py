#!/usr/bin/env python3
"""Export NIfTI volumes into per-case 2D slice folders.

Usage:
  python3 scripts/extract_cases_from_nii.py
  python3 scripts/extract_cases_from_nii.py --input-dir raw_data --output-dir cases --overwrite
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export each NIfTI file under raw_data into a case folder with Z-axis slices and metadata."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("raw_data"),
        help="Directory containing source .nii or .nii.gz files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("cases"),
        help="Directory where exported case folders will be written.",
    )
    parser.add_argument(
        "--image-format",
        choices=("png",),
        default="png",
        help="Image format for exported slices.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing case directories.",
    )
    parser.add_argument(
        "--window-center",
        type=float,
        default=-600.0,
        help="Window center used for all slices in the case. Default is lung window center.",
    )
    parser.add_argument(
        "--window-width",
        type=float,
        default=1500.0,
        help="Window width used for all slices in the case. Default is lung window width.",
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


def list_nifti_files(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and (path.name.endswith(".nii") or path.name.endswith(".nii.gz"))
    )


def case_id_from_path(path: Path) -> str:
    name = path.name
    if name.endswith(".nii.gz"):
        return name[:-7]
    if name.endswith(".nii"):
        return name[:-4]
    return path.stem


def apply_window_to_uint8(np_module, slice_array, window_center: float, window_width: float):
    slice_float = np_module.asarray(slice_array, dtype=np_module.float32)
    window_min = float(window_center - window_width / 2.0)
    window_max = float(window_center + window_width / 2.0)
    if window_max <= window_min:
        raise ValueError("window_width must be greater than 0")
    clipped = np_module.clip(slice_float, window_min, window_max)
    scaled = (clipped - window_min) / (window_max - window_min)
    return (scaled * 255.0).clip(0, 255).astype(np_module.uint8), window_min, window_max


def to_native_number(value):
    if hasattr(value, "item"):
        return value.item()
    return value


def normalize_vector(np_module, vector):
    array = np_module.asarray(vector, dtype=np_module.float32)
    norm = float(np_module.linalg.norm(array))
    if norm == 0:
        raise ValueError("Cannot normalize zero-length vector")
    return [float(v) for v in (array / norm)]


def export_case(
    nifti_path: Path,
    output_dir: Path,
    image_format: str,
    overwrite: bool,
    window_center: float,
    window_width: float,
) -> str:
    nib, np, Image = require_dependencies()

    case_id = case_id_from_path(nifti_path)
    case_dir = output_dir / case_id
    slices_dir = case_dir / "slices"

    if case_dir.exists():
        if not overwrite:
            return f"skip {case_id}: output already exists"
        shutil.rmtree(case_dir)

    case_dir.mkdir(parents=True, exist_ok=True)
    slices_dir.mkdir(parents=True, exist_ok=True)

    image = nib.load(str(nifti_path))
    data = image.get_fdata(dtype=np.float32)

    if data.ndim != 3:
        raise ValueError(f"{nifti_path.name} is {data.ndim}D; only 3D volumes are supported.")

    x_dim, y_dim, z_dim = map(int, data.shape)
    window_min = float(window_center - window_width / 2.0)
    window_max = float(window_center + window_width / 2.0)

    for z_index in range(z_dim):
        slice_array = data[:, :, z_index]
        normalized_slice, _, _ = apply_window_to_uint8(np, slice_array, window_center, window_width)
        instance_number = z_index + 1
        output_name = f"slice_{instance_number:04d}.{image_format}"
        output_path = slices_dir / output_name
        Image.fromarray(normalized_slice, mode="L").save(output_path)

    header = image.header
    zooms = [float(to_native_number(v)) for v in header.get_zooms()[:3]]
    affine_matrix = image.affine
    row_direction = normalize_vector(np, affine_matrix[:3, 0])
    col_direction = normalize_vector(np, affine_matrix[:3, 1])
    image_orientation_patient = row_direction + col_direction
    first_image_position_patient = [float(v) for v in affine_matrix[:3, 3]]
    metadata = {
        "case_id": case_id,
        "ImageFile": {
            "directory": str(slices_dir.resolve()),
            "format": image_format,
            "pattern": f"slice_{{InstanceNumber:04d}}.{image_format}",
            "count": z_dim,
        },
        "InstanceNumber": {
            "start": 1,
            "end": z_dim,
            "step": 1,
            "sort_order": "ascending",
        },
        "ImagePositionPatient": first_image_position_patient,
        "ImageOrientationPatient": image_orientation_patient,
        "PixelSpacing": zooms[:2],
        "Rows": y_dim,
        "Columns": x_dim,
        "SliceThickness": zooms[2],
        "WindowCenter": window_center,
        "WindowWidth": window_width,
    }

    metadata_path = case_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"done {case_id}: {z_dim} slices"


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not input_dir.exists():
        raise SystemExit(f"Input directory not found: {input_dir}")

    nifti_files = list_nifti_files(input_dir)
    if not nifti_files:
        raise SystemExit(f"No .nii or .nii.gz files found under: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for nifti_path in nifti_files:
        results.append(
            export_case(
                nifti_path,
                output_dir,
                args.image_format,
                args.overwrite,
                args.window_center,
                args.window_width,
            )
        )

    for line in results:
        print(line)


if __name__ == "__main__":
    main()
