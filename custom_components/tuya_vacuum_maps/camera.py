"""Home Assistant entity to display the map from a vacuum."""

import colorsys
import io
import logging
import urllib.request
from collections import Counter
from datetime import timedelta
from typing import Any

import tuya_vacuum
from PIL import Image, ImageColor, ImageDraw
from homeassistant.components.camera import Camera, ENTITY_ID_FORMAT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from tuya_vacuum.const import ORIGIN_MAP_COLOR
from tuya_vacuum.lz4 import uncompress
from tuya_vacuum.map.layout import Layout
from tuya_vacuum.map.map import Map
from tuya_vacuum.map.path import Path as PathMap
from tuya_vacuum.utils import chunks, combine_high_low_to_int, create_format_path, deal_pl

SCAN_INTERVAL = timedelta(seconds=10)

SCALE = 8
PATH_ROTATION_DEGREES = -90

ROOM_COLORS = [ImageColor.getcolor(color, "RGB") for color in ORIGIN_MAP_COLOR]
BG_COLOR = (0, 110, 230)
WALL_COLOR = (52, 52, 52)
PATH_COLOR = (255, 255, 255)
CHARGER_COLOR = (0, 128, 0)
VACUUM_COLOR = (0, 0, 255)
MARKER_OUTLINE = (255, 255, 255)

_LOGGER = logging.getLogger(__name__)


def _create_vacuum(
    origin: str, client_id: str, client_secret: str, device_id: str
) -> Any:
    """Create a vacuum object for supported tuya_vacuum versions."""
    vacuum_cls = getattr(tuya_vacuum, "TuyaVacuum", None)
    if vacuum_cls is None:
        vacuum_cls = getattr(tuya_vacuum, "Vacuum", None)
    if vacuum_cls is None:
        raise AttributeError("tuya_vacuum has no TuyaVacuum or Vacuum class")

    return vacuum_cls(origin, client_id, client_secret, device_id)


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

    available = [n for n in dir(vacuum) if "map" in n.lower() or "fetch" in n.lower()]
    raise AttributeError(
        f"{type(vacuum).__name__} has no supported map method. Available: {available}"
    )


def _fetch_raw_maps(vacuum: Any, device_id: str) -> tuple[bytes | None, bytes | None]:
    """Fetch raw map binaries directly from Tuya Cloud API."""
    api = getattr(vacuum, "api", None)
    if api is None or not callable(getattr(api, "request", None)):
        raise AttributeError("Vacuum object has no compatible API client for fallback")

    endpoint = f"/v1.0/users/sweepers/file/{device_id}/realtime-map"
    response = api.request("GET", endpoint)
    map_parts = response.get("result", [])

    layout_data = None
    path_data = None

    for map_part in map_parts:
        map_url = map_part.get("map_url")
        map_type = map_part.get("map_type")
        if not map_url:
            continue

        client = getattr(api, "client", None)
        if client is not None and callable(getattr(client, "request", None)):
            map_data = client.request("GET", map_url).content
        else:
            with urllib.request.urlopen(map_url, timeout=10) as response_obj:
                map_data = response_obj.read()

        if map_type == 0:
            layout_data = map_data
        elif map_type == 1:
            path_data = map_data
        else:
            _LOGGER.debug("Ignoring unsupported map type: %s", map_type)

    return layout_data, path_data


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


def _image_to_png_bytes(image: Image.Image) -> bytes:
    """Convert a PIL Image to PNG bytes."""
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()


def _to_png_bytes(vacuum_map: Any) -> bytes:
    """Convert map object returned by library into PNG bytes."""
    if isinstance(vacuum_map, (bytes, bytearray)):
        return bytes(vacuum_map)

    to_image = getattr(vacuum_map, "to_image", None)
    if callable(to_image):
        try:
            image = to_image()
            return _image_to_png_bytes(image)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Standard map.to_image failed: %s", err)

        _ensure_layout_defaults(vacuum_map)
        try:
            image = to_image()
            return _image_to_png_bytes(image)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Retry map.to_image with defaults failed: %s", err)

        layout_raw = getattr(getattr(vacuum_map, "layout", None), "raw", None)
        path_raw = getattr(getattr(vacuum_map, "path", None), "raw", None)
        if isinstance(layout_raw, (bytes, bytearray)) and isinstance(
            path_raw, (bytes, bytearray)
        ):
            image = _render_manual_fallback(bytes(layout_raw), bytes(path_raw))
            return _image_to_png_bytes(image)

    image_attr = getattr(vacuum_map, "image", None)
    if image_attr is not None and hasattr(image_attr, "save"):
        return _image_to_png_bytes(image_attr)

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


def _decode_layout_pixels_fallback(
    layout_data: bytes, width: int, height: int
) -> tuple[list[int], int]:
    """Decode a layout payload with dynamic offset detection."""
    area = width * height
    best_decoded = None
    best_distance = 10**9

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
        values = [255] * area
        return values, 255

    if len(best_decoded) == area + 1:
        payload = best_decoded[1:]
    else:
        payload = best_decoded[:area]

    values = list(payload)
    bg_value = Counter(values).most_common(1)[0][0]
    return values, bg_value


def _parse_path_points_fallback(path_data: bytes) -> list[dict[str, float]]:
    """Parse path points from raw bytes when Path parser fails."""
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


def _compute_crop_box(
    layout_values: list[int],
    width: int,
    height: int,
    bg_value: int,
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

    if value % 4 == 0:
        room_id = value // 4
        base = ROOM_COLORS[room_id % len(ROOM_COLORS)]
        h, l, s = colorsys.rgb_to_hls(*(channel / 255.0 for channel in base))
        muted = colorsys.hls_to_rgb(h, min(0.78, l), max(0.35, s * 0.65))
        return tuple(int(channel * 255) for channel in muted)

    if value % 4 in (1, 3):
        return WALL_COLOR

    if value % 4 == 2:
        return (220, 180, 60)

    return (30, 30, 30)


def _draw_marker(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    outer_radius: int,
    inner_radius: int,
    inner_color: tuple[int, int, int],
) -> None:
    """Draw a two-ring map marker."""
    draw.ellipse(
        (
            center_x - outer_radius,
            center_y - outer_radius,
            center_x + outer_radius,
            center_y + outer_radius,
        ),
        fill=MARKER_OUTLINE,
    )
    draw.ellipse(
        (
            center_x - inner_radius,
            center_y - inner_radius,
            center_x + inner_radius,
            center_y + inner_radius,
        ),
        fill=inner_color,
    )


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
        _LOGGER.debug("Path parser failed, using raw fallback points: %s", err)
        points = _parse_path_points_fallback(path_data)

    layout_values, bg_value = _decode_layout_pixels_fallback(layout_data, width, height)
    min_x, min_y, max_x, max_y = _compute_crop_box(
        layout_values, width, height, bg_value, origin_x, origin_y
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
        coordinates.append((point["x"] - min_x) * SCALE)
        coordinates.append((point["y"] - min_y) * SCALE)

    charger_x = (origin_x - min_x) * SCALE
    charger_y = (origin_y - min_y) * SCALE
    coordinates = _rotate_polyline_coordinates(
        coordinates, charger_x, charger_y, PATH_ROTATION_DEGREES
    )

    if len(coordinates) >= 4:
        draw.line(coordinates, fill=PATH_COLOR, width=max(2, SCALE // 2), joint="curve")

    _draw_marker(
        draw=draw,
        center_x=charger_x,
        center_y=charger_y,
        outer_radius=max(3, SCALE * 2),
        inner_radius=max(2, SCALE * 2 - 4),
        inner_color=CHARGER_COLOR,
    )

    if len(coordinates) >= 2:
        vacuum_x, vacuum_y = coordinates[-2], coordinates[-1]
        _draw_marker(
            draw=draw,
            center_x=vacuum_x,
            center_y=vacuum_y,
            outer_radius=max(3, SCALE * 2),
            inner_radius=max(2, SCALE * 2 - 4),
            inner_color=VACUUM_COLOR,
        )

    return image


def _render_from_raw(layout_data: bytes, path_data: bytes) -> bytes:
    """Render final PNG from raw layout/path binaries."""
    try:
        vacuum_map = Map(Layout(layout_data), PathMap(path_data))
        return _to_png_bytes(vacuum_map)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Standard render from raw failed, using fallback: %s", err)
        image = _render_manual_fallback(layout_data, path_data)
        return _image_to_png_bytes(image)


def _fetch_map_image(
    origin: str, client_id: str, client_secret: str, device_id: str
) -> bytes:
    """Fetch and render map image using blocking library calls."""
    vacuum = _create_vacuum(origin, client_id, client_secret, device_id)

    try:
        vacuum_map = _fetch_realtime_map(vacuum)
        return _to_png_bytes(vacuum_map)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.warning("Standard map fetch/render failed, using raw fallback: %s", err)

    layout_data, path_data = _fetch_raw_maps(vacuum, device_id)
    if layout_data is None or path_data is None:
        raise RuntimeError("Could not retrieve layout/path map data for fallback")
    return _render_from_raw(layout_data, path_data)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add camera for passed config_entry in HA."""
    _LOGGER.debug("Async setup entry")
    name = config_entry.title
    entity_id = generate_entity_id(ENTITY_ID_FORMAT, name, hass=hass)
    origin = config_entry.data["server"]
    client_id = config_entry.data["client_id"]
    client_secret = config_entry.data["client_secret"]
    device_id = config_entry.data["device_id"]

    _LOGGER.debug("Adding entities")
    async_add_entities(
        [VacuumMapCamera(origin, client_id, client_secret, device_id, entity_id, hass)]
    )
    _LOGGER.debug("Done")


class VacuumMapCamera(Camera):
    """Home Assistant entity to display the map from a vacuum."""

    def __init__(self, origin, client_id, client_secret, device_id, entity_id, hass):
        """Initialize the camera."""
        super().__init__()
        self._origin = origin
        self._client_id = client_id
        self._client_secret = client_secret
        self._device_id = device_id
        self._image = None
        self.hass = hass

        self.content_type = "image/png"
        self.entity_id = entity_id
        self._attr_is_streaming = False

    def update(self) -> None:
        """Update the image."""
        raise NotImplementedError

    async def async_update(self) -> None:
        """Update the image."""
        _LOGGER.debug("Updating image")
        try:
            self._image = await self.hass.async_add_executor_job(
                _fetch_map_image,
                self._origin,
                self._client_id,
                self._client_secret,
                self._device_id,
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to update vacuum map image")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of the image."""
        return self._image

    @property
    def should_poll(self) -> bool:
        return True
