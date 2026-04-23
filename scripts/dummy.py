"""Download realtime map data and render final PNG like Home Assistant camera."""

import argparse
import colorsys
import io
import os
from collections import Counter
from pathlib import Path
from typing import Any

import requests
import tuya_vacuum
from PIL import Image, ImageColor, ImageDraw
from tuya_vacuum.const import ORIGIN_MAP_COLOR
from tuya_vacuum.lz4 import uncompress
from tuya_vacuum.map.layout import Layout
from tuya_vacuum.map.map import Map
from tuya_vacuum.map.path import Path as PathMap
from tuya_vacuum.tuya import TuyaCloudAPI
from tuya_vacuum.utils import chunks, combine_high_low_to_int, create_format_path, deal_pl

DEFAULT_SERVER = "https://openapi.tuyaus.com"
SCALE = 8
PATH_ROTATION_DEGREES = -90
ROOM_COLORS = [ImageColor.getcolor(color, "RGB") for color in ORIGIN_MAP_COLOR]
BG_COLOR = (0, 110, 230)
WALL_COLOR = (52, 52, 52)


def _create_vacuum(
    server: str, client_id: str, client_secret: str, device_id: str
) -> Any:
    """Create a vacuum object across tuya_vacuum API variants."""
    vacuum_cls = getattr(tuya_vacuum, "TuyaVacuum", None)
    if vacuum_cls is None:
        vacuum_cls = getattr(tuya_vacuum, "Vacuum", None)
    if vacuum_cls is None:
        raise AttributeError("tuya_vacuum has no TuyaVacuum or Vacuum class")

    return vacuum_cls(server, client_id, client_secret, device_id)


def _fetch_realtime_map(vacuum: Any) -> Any:
    """Fetch realtime map across library API variants."""
    for method_name in (
        "fetch_realtime_map",
        "get_realtime_map",
        "get_realtime_maps",
        "fetch_map",
        "get_map",
        "realtime_map",
    ):
        method = getattr(vacuum, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, list):
                if not result:
                    raise ValueError("Map request returned an empty list")
                return result[0]
            return result
    raise AttributeError(f"{type(vacuum).__name__} has no supported map method")


def _ensure_layout_defaults(vacuum_map: Any) -> None:
    """Populate missing layout attributes so to_image() can run."""
    layout = getattr(vacuum_map, "layout", None)
    if layout is None:
        return

    if not hasattr(layout, "rooms"):
        layout.rooms = []

    if not hasattr(layout, "_map_data_array"):
        width = int(getattr(layout, "width", 0) or 0)
        height = int(getattr(layout, "height", 0) or 0)
        if width > 0 and height > 0:
            layout._map_data_array = bytes([0]) * (width * height)


def _render_fallback_image(vacuum_map: Any) -> Image.Image:
    """Render a fallback image when map.to_image() fails on unknown formats."""
    layout = getattr(vacuum_map, "layout", None)
    path = getattr(vacuum_map, "path", None)

    width = int(getattr(layout, "width", 0) or 0)
    height = int(getattr(layout, "height", 0) or 0)
    origin_x = int(getattr(layout, "origin_x", 0) or 0)
    origin_y = int(getattr(layout, "origin_y", 0) or 0)

    if width <= 0 or height <= 0:
        raise ValueError("Invalid layout dimensions for fallback render")

    scale = 8
    image = Image.new("RGBA", (width * scale, height * scale), (0, 110, 230, 255))

    to_path_image = getattr(path, "to_image", None)
    if callable(to_path_image):
        try:
            path_image = to_path_image(width, height, (origin_x, origin_y))
            image.paste(path_image, mask=path_image)
        except Exception as err:  # pylint: disable=broad-except
            print(f"[WARN] Failed to overlay path on fallback image: {err}")

    return image.convert("RGB")


def _to_png_bytes(vacuum_map: Any) -> bytes:
    """Convert map object returned by library into PNG bytes."""
    if isinstance(vacuum_map, (bytes, bytearray)):
        return bytes(vacuum_map)

    to_image = getattr(vacuum_map, "to_image", None)
    if callable(to_image):
        try:
            image = to_image()
        except Exception as err:  # pylint: disable=broad-except
            print(f"[WARN] map.to_image() failed, trying defaults: {err}")
            _ensure_layout_defaults(vacuum_map)
            try:
                image = to_image()
            except Exception as err2:  # pylint: disable=broad-except
                print(f"[WARN] Retry failed, using fallback renderer: {err2}")
                image = _render_fallback_image(vacuum_map)

        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="PNG")
        return img_byte_arr.getvalue()

    raise TypeError(f"Unsupported map object type: {type(vacuum_map).__name__}")


def _parse_layout_header_fallback(layout_data: bytes) -> dict[str, int]:
    """Parse just enough layout header fields for manual rendering."""
    if len(layout_data) < 24:
        raise ValueError("layout.bin is too short to parse header")

    header_hex = layout_data.hex()[:48]
    header_values = [
        combine_high_low_to_int(pair[0], pair[1])
        for pair in chunks([int(header_hex[i : i + 2], 16) for i in range(0, 48, 2)], 2)
    ]
    width = int(header_values[2])
    height = int(header_values[3])
    origin_x = int(round(header_values[4] / 10))
    origin_y = int(round(header_values[5] / 10))

    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid dimensions in fallback header: {width}x{height}")

    return {
        "width": width,
        "height": height,
        "origin_x": origin_x,
        "origin_y": origin_y,
    }


def _parse_path_points_fallback(path_data: bytes) -> list[dict[str, float]]:
    """Parse path points from raw bytes when Path parser fails."""
    # Path header size used by the upstream lib.
    header_len = 26
    if len(path_data) <= header_len:
        return []

    raw = path_data[header_len:]
    format_path_point = create_format_path(reverse_y=True, hide_path=True)
    points: list[dict[str, float]] = []
    for index in range(0, len(raw) - 3, 4):
        high_x, low_x, high_y, low_y = raw[index : index + 4]
        x = deal_pl(combine_high_low_to_int(high_x, low_x))
        y = deal_pl(combine_high_low_to_int(high_y, low_y))
        points.append(format_path_point(x, y))

    return points


def _render_manual_fallback(layout_data: bytes, path_data: bytes) -> Image.Image:
    """Render map image directly from raw binaries without Layout parser support."""
    header = _parse_layout_header_fallback(layout_data)
    width = header["width"]
    height = header["height"]
    origin_x = header["origin_x"]
    origin_y = header["origin_y"]

    try:
        path_obj = PathMap(path_data)
        points = getattr(path_obj, "_path_data", [])
    except Exception as err:  # pylint: disable=broad-except
        print(f"[WARN] Path parser failed, using raw fallback points: {err}")
        points = _parse_path_points_fallback(path_data)

    layout_values, bg_value = _decode_layout_pixels_fallback(layout_data, width, height)
    min_x, min_y, max_x, max_y = _compute_crop_box(
        layout_values, width, height, bg_value, points, origin_x, origin_y
    )

    cropped_width = max_x - min_x + 1
    cropped_height = max_y - min_y + 1
    image = Image.new("RGB", (cropped_width * SCALE, cropped_height * SCALE), BG_COLOR)
    draw = ImageDraw.Draw(image)

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            value = layout_values[x + y * width]
            color = _color_for_layout_pixel(value, bg_value)
            if color == BG_COLOR:
                continue
            left = (x - min_x) * SCALE
            top = (y - min_y) * SCALE
            draw.rectangle(
                [(left, top), (left + SCALE - 1, top + SCALE - 1)],
                fill=color,
            )

    filtered_points = _sanitize_path_points(points, width, height, origin_x, origin_y)

    coordinates: list[float] = []
    for point in filtered_points:
        x = (point["x"] - min_x) * SCALE
        y = (point["y"] - min_y) * SCALE
        coordinates.append(x)
        coordinates.append(y)

    cx = (origin_x - min_x) * SCALE
    cy = (origin_y - min_y) * SCALE
    coordinates = _rotate_polyline_coordinates(
        coordinates, cx, cy, PATH_ROTATION_DEGREES
    )

    if len(coordinates) >= 4:
        draw.line(coordinates, fill="white", width=max(2, SCALE // 2), joint="curve")

    # Charger marker
    draw.circle((cx, cy), max(3, SCALE * 2), fill="white")
    draw.circle((cx, cy), max(2, SCALE * 2 - 4), fill="green")

    # Vacuum marker
    if len(coordinates) >= 2:
        vx, vy = coordinates[-2], coordinates[-1]
        draw.circle((vx, vy), max(3, SCALE * 2), fill="white")
        draw.circle((vx, vy), max(2, SCALE * 2 - 4), fill="blue")

    return image


def _sanitize_path_points(
    points: list[dict[str, float]],
    width: int,
    height: int,
    origin_x: int,
    origin_y: int,
) -> list[dict[str, float]]:
    """Normalize path points into map coordinates and keep points inside bounds."""
    output: list[dict[str, float]] = []

    for point in points:
        px = float(point["x"]) + origin_x
        py = float(point["y"]) + origin_y

        if not (0 <= px < width and 0 <= py < height):
            continue

        output.append({"x": px, "y": py})

    return output


def _rotate_polyline_coordinates(
    coordinates: list[float], pivot_x: float, pivot_y: float, degrees: int
) -> list[float]:
    """Rotate polyline coordinates around a pivot in 90-degree steps."""
    normalized = degrees % 360
    if normalized == 0 or not coordinates:
        return coordinates

    rotated: list[float] = []
    for index in range(0, len(coordinates) - 1, 2):
        x = coordinates[index]
        y = coordinates[index + 1]
        dx = x - pivot_x
        dy = y - pivot_y

        if normalized == 90:
            rx, ry = pivot_x + dy, pivot_y - dx
        elif normalized == 180:
            rx, ry = pivot_x - dx, pivot_y - dy
        elif normalized == 270:
            rx, ry = pivot_x - dy, pivot_y + dx
        else:
            raise ValueError(f"Unsupported PATH_ROTATION_DEGREES={degrees}")

        rotated.append(rx)
        rotated.append(ry)

    return rotated


def _decode_layout_pixels_fallback(
    layout_data: bytes, width: int, height: int
) -> tuple[list[int], int]:
    """Decode a layout payload with dynamic offset detection."""
    area = width * height
    best_decoded = None
    best_distance = 10**9

    # Newer layout formats can shift the compressed payload; detect the offset.
    for offset in range(0, min(96, len(layout_data))):
        try:
            decoded = uncompress(layout_data[offset:])
        except Exception:  # pylint: disable=broad-except
            continue

        if len(decoded) < area:
            continue
        distance = abs(len(decoded) - area)
        if distance < best_distance:
            best_distance = distance
            best_decoded = decoded

    if best_decoded is None:
        # Last-resort fallback: empty map body.
        values = [255] * area
        return values, 255

    if len(best_decoded) == area + 1:
        payload = best_decoded[1:]
    else:
        payload = best_decoded[:area]

    values = list(payload)
    bg_value = Counter(values).most_common(1)[0][0]
    return values, bg_value


def _compute_crop_box(
    layout_values: list[int],
    width: int,
    height: int,
    bg_value: int,
    points: list[dict[str, float]],
    origin_x: int,
    origin_y: int,
) -> tuple[int, int, int, int]:
    """Compute a centered crop around layout content."""
    xs: list[int] = []
    ys: list[int] = []

    for y in range(height):
        for x in range(width):
            if layout_values[x + y * width] != bg_value:
                xs.append(x)
                ys.append(y)

    if not xs or not ys:
        return 0, 0, width - 1, height - 1

    xs.append(origin_x)
    ys.append(origin_y)

    pad = 6
    min_x = max(0, min(xs) - pad)
    min_y = max(0, min(ys) - pad)
    max_x = min(width - 1, max(xs) + pad)
    max_y = min(height - 1, max(ys) + pad)
    return min_x, min_y, max_x, max_y


def _color_for_layout_pixel(value: int, bg_value: int) -> tuple[int, int, int]:
    """Map raw layout byte to RGB with room-friendly colors."""
    if value == bg_value or value == 255:
        return BG_COLOR

    # Tuya v1/v47-like encoding: room IDs are usually multiples of 4.
    if value % 4 == 0:
        room_id = value // 4
        base = ROOM_COLORS[room_id % len(ROOM_COLORS)]
        # Slightly desaturate for readability.
        h, l, s = colorsys.rgb_to_hls(*(channel / 255.0 for channel in base))
        muted = colorsys.hls_to_rgb(h, min(0.78, l), max(0.35, s * 0.65))
        return tuple(int(channel * 255) for channel in muted)

    if value % 4 in (1, 3):
        return WALL_COLOR

    # Rare marker values.
    if value % 4 == 2:
        return (220, 180, 60)

    return (30, 30, 30)


def _download_raw_maps(
    server: str, client_id: str, client_secret: str, device_id: str, output_dir: Path
) -> tuple[bytes | None, bytes | None]:
    """Download layout/path map binaries from Tuya Cloud and save to disk."""
    endpoint = f"/v1.0/users/sweepers/file/{device_id}/realtime-map"
    api = TuyaCloudAPI(origin=server, client_id=client_id, client_secret=client_secret)
    response = api.request("GET", endpoint)
    maps = response.get("result", [])

    layout_data = None
    path_data = None

    for index, map_part in enumerate(maps):
        map_url = map_part.get("map_url")
        map_type = map_part.get("map_type")
        if not map_url:
            continue

        map_data = requests.get(map_url, timeout=10).content

        if map_type == 0:
            layout_data = map_data
            (output_dir / "layout.bin").write_bytes(map_data)
            print("[INFO] layout.bin salvo")
        elif map_type == 1:
            path_data = map_data
            (output_dir / "path.bin").write_bytes(map_data)
            print("[INFO] path.bin salvo")
        else:
            unknown_file = output_dir / f"map_type_{map_type}_{index}.bin"
            unknown_file.write_bytes(map_data)
            print(f"[WARN] map_type={map_type} salvo em {unknown_file.name}")

    return layout_data, path_data


def _render_from_raw(layout_data: bytes, path_data: bytes) -> bytes:
    """Render final PNG from raw layout/path binaries."""
    try:
        vacuum_map = Map(Layout(layout_data), PathMap(path_data))
        return _to_png_bytes(vacuum_map)
    except Exception as err:  # pylint: disable=broad-except
        print(f"[WARN] Standard render failed, using manual fallback: {err}")
        image = _render_manual_fallback(layout_data, path_data)
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="PNG")
        return img_byte_arr.getvalue()


def parse_args() -> argparse.Namespace:
    """Parse CLI args with env var fallback."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("TUYA_SERVER", DEFAULT_SERVER))
    parser.add_argument("--client-id", default=os.getenv("TUYA_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("TUYA_CLIENT_SECRET"))
    parser.add_argument("--device-id", default=os.getenv("TUYA_DEVICE_ID"))
    parser.add_argument(
        "--output-dir",
        default=os.getenv("TUYA_OUTPUT_DIR", "."),
        help="Diretório onde serão salvos layout.bin, path.bin e final_map.png",
    )
    parser.add_argument(
        "--output-image",
        default=os.getenv("TUYA_OUTPUT_IMAGE", "final_map.png"),
        help="Nome do arquivo PNG final",
    )
    return parser.parse_args()


def main() -> None:
    """Run map download and render pipeline."""
    args = parse_args()

    missing = [
        name
        for name, value in (
            ("TUYA_CLIENT_ID/--client-id", args.client_id),
            ("TUYA_CLIENT_SECRET/--client-secret", args.client_secret),
            ("TUYA_DEVICE_ID/--device-id", args.device_id),
        )
        if not value
    ]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Parâmetros obrigatórios ausentes: {joined}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_image = output_dir / args.output_image

    print(f"[INFO] Baixando mapas para {output_dir}")
    layout_data, path_data = _download_raw_maps(
        args.server, args.client_id, args.client_secret, args.device_id, output_dir
    )

    png_bytes = None
    if layout_data is not None and path_data is not None:
        png_bytes = _render_from_raw(layout_data, path_data)
    else:
        print("[WARN] layout.bin/path.bin incompletos, tentando via Vacuum API")
        vacuum = _create_vacuum(
            args.server, args.client_id, args.client_secret, args.device_id
        )
        vacuum_map = _fetch_realtime_map(vacuum)
        png_bytes = _to_png_bytes(vacuum_map)

    output_image.write_bytes(png_bytes)
    print(f"[OK] PNG final gerado em: {output_image}")


if __name__ == "__main__":
    main()
