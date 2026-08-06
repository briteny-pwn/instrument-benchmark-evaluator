from __future__ import annotations

import math
from dataclasses import dataclass

from .metrics import TriangleMesh, Vec


EPSILON = 1e-12


def _add(left: Vec, right: Vec) -> Vec:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _sub(left: Vec, right: Vec) -> Vec:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _scale(value: Vec, factor: float) -> Vec:
    return tuple(item * factor for item in value)  # type: ignore[return-value]


def _dot(left: Vec, right: Vec) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: Vec, right: Vec) -> Vec:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: Vec) -> float:
    return math.sqrt(_dot(value, value))


@dataclass(frozen=True)
class ContactMetrics:
    minimum_distance_um: float
    section_um: float
    connected: bool


def contact_metrics(
    first: TriangleMesh,
    second: TriangleMesh,
    *,
    epsilon_um: float,
    min_section_um: float,
) -> ContactMetrics:
    if epsilon_um < 0 or min_section_um < 0:
        raise ValueError("contact thresholds must not be negative")
    if not first.bounds.overlaps(second.bounds, padding=epsilon_um):
        distance = _bounds_distance(first, second)
        return ContactMetrics(distance, 0.0, False)
    minimum = math.inf
    for triangle_a in first.triangles:
        for triangle_b in second.triangles:
            minimum = min(minimum, _triangle_distance(triangle_a, triangle_b))
            if minimum <= EPSILON:
                break
        if minimum <= EPSILON:
            break
    contained = False
    if minimum > EPSILON:
        contained = any(_point_inside_mesh(vertex, second) for vertex in first.vertices)
        contained = contained or any(
            _point_inside_mesh(vertex, first) for vertex in second.vertices
        )
        if contained:
            minimum = 0.0
    section = _contact_section(first, second, epsilon_um) if minimum <= epsilon_um else 0.0
    return ContactMetrics(
        minimum_distance_um=minimum,
        section_um=section,
        connected=minimum <= epsilon_um and section + EPSILON >= min_section_um,
    )


def _bounds_distance(first: TriangleMesh, second: TriangleMesh) -> float:
    gaps = []
    for index in range(3):
        if first.bounds.maximum[index] < second.bounds.minimum[index]:
            gaps.append(second.bounds.minimum[index] - first.bounds.maximum[index])
        elif second.bounds.maximum[index] < first.bounds.minimum[index]:
            gaps.append(first.bounds.minimum[index] - second.bounds.maximum[index])
        else:
            gaps.append(0.0)
    return math.sqrt(sum(gap * gap for gap in gaps))


def _contact_section(
    first: TriangleMesh, second: TriangleMesh, epsilon_um: float
) -> float:
    extents = list(first.bounds.overlap_extents(second.bounds))
    for index, value in enumerate(extents):
        if value == 0.0:
            projected_overlap = min(first.bounds.size[index], second.bounds.size[index])
            extents[index] = min(projected_overlap, epsilon_um)
    extents.sort(reverse=True)
    return math.sqrt(max(0.0, extents[0] * extents[1]))


def _triangle_distance(first: tuple[Vec, Vec, Vec], second: tuple[Vec, Vec, Vec]) -> float:
    edges_a = ((first[0], first[1]), (first[1], first[2]), (first[2], first[0]))
    edges_b = ((second[0], second[1]), (second[1], second[2]), (second[2], second[0]))
    if any(_segment_intersects_triangle(start, end, second) for start, end in edges_a):
        return 0.0
    if any(_segment_intersects_triangle(start, end, first) for start, end in edges_b):
        return 0.0
    minimum = min(
        *(_point_triangle_distance(point, second) for point in first),
        *(_point_triangle_distance(point, first) for point in second),
    )
    for start_a, end_a in edges_a:
        for start_b, end_b in edges_b:
            minimum = min(
                minimum,
                _segment_distance(start_a, end_a, start_b, end_b),
            )
    return minimum


def _segment_intersects_triangle(
    start: Vec, end: Vec, triangle: tuple[Vec, Vec, Vec]
) -> bool:
    direction = _sub(end, start)
    first_edge = _sub(triangle[1], triangle[0])
    second_edge = _sub(triangle[2], triangle[0])
    perpendicular = _cross(direction, second_edge)
    determinant = _dot(first_edge, perpendicular)
    if abs(determinant) <= EPSILON:
        return False
    inverse = 1.0 / determinant
    offset = _sub(start, triangle[0])
    u = inverse * _dot(offset, perpendicular)
    if u < -EPSILON or u > 1.0 + EPSILON:
        return False
    cross_offset = _cross(offset, first_edge)
    v = inverse * _dot(direction, cross_offset)
    if v < -EPSILON or u + v > 1.0 + EPSILON:
        return False
    distance = inverse * _dot(second_edge, cross_offset)
    return -EPSILON <= distance <= 1.0 + EPSILON


def _point_inside_mesh(point: Vec, mesh: TriangleMesh) -> bool:
    direction: Vec = (1.0, 0.371390676, 0.529150262)
    intersections: set[float] = set()
    for triangle in mesh.triangles:
        distance = _ray_triangle_distance(point, direction, triangle)
        if distance is not None and distance > EPSILON:
            intersections.add(round(distance, 9))
    return len(intersections) % 2 == 1


def _ray_triangle_distance(
    origin: Vec, direction: Vec, triangle: tuple[Vec, Vec, Vec]
) -> float | None:
    first_edge = _sub(triangle[1], triangle[0])
    second_edge = _sub(triangle[2], triangle[0])
    perpendicular = _cross(direction, second_edge)
    determinant = _dot(first_edge, perpendicular)
    if abs(determinant) <= EPSILON:
        return None
    inverse = 1.0 / determinant
    offset = _sub(origin, triangle[0])
    u = inverse * _dot(offset, perpendicular)
    if u < -EPSILON or u > 1.0 + EPSILON:
        return None
    cross_offset = _cross(offset, first_edge)
    v = inverse * _dot(direction, cross_offset)
    if v < -EPSILON or u + v > 1.0 + EPSILON:
        return None
    return inverse * _dot(second_edge, cross_offset)


def _point_triangle_distance(point: Vec, triangle: tuple[Vec, Vec, Vec]) -> float:
    a, b, c = triangle
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(point, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return _norm(ap)
    bp = _sub(point, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return _norm(bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        factor = d1 / (d1 - d3)
        return _norm(_sub(point, _add(a, _scale(ab, factor))))
    cp = _sub(point, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return _norm(cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        factor = d2 / (d2 - d6)
        return _norm(_sub(point, _add(a, _scale(ac, factor))))
    va = d3 * d6 - d5 * d4
    if va <= 0 and d4 - d3 >= 0 and d5 - d6 >= 0:
        factor = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return _norm(_sub(point, _add(b, _scale(_sub(c, b), factor))))
    denominator = 1.0 / (va + vb + vc)
    v, w = vb * denominator, vc * denominator
    projection = _add(a, _add(_scale(ab, v), _scale(ac, w)))
    return _norm(_sub(point, projection))


def _segment_distance(p1: Vec, q1: Vec, p2: Vec, q2: Vec) -> float:
    d1, d2, r = _sub(q1, p1), _sub(q2, p2), _sub(p1, p2)
    a, e, f = _dot(d1, d1), _dot(d2, d2), _dot(d2, r)
    if a <= EPSILON and e <= EPSILON:
        return _norm(r)
    if a <= EPSILON:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = _dot(d1, r)
        if e <= EPSILON:
            t = 0.0
            s = min(max(-c / a, 0.0), 1.0)
        else:
            b = _dot(d1, d2)
            denominator = a * e - b * b
            s = 0.0 if denominator == 0 else min(max((b * f - c * e) / denominator, 0.0), 1.0)
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(max(-c / a, 0.0), 1.0)
            elif t > 1.0:
                t = 1.0
                s = min(max((b - c) / a, 0.0), 1.0)
    closest = _sub(_add(p1, _scale(d1, s)), _add(p2, _scale(d2, t)))
    return _norm(closest)
