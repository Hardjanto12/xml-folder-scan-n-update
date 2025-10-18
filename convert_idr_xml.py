#!/usr/bin/env python3
"""
convert_idr_xml.py

Convert Nuctech-style IDR XML into a JSON payload with:
{
  "ftp_path": "...",
  "image_msg": "<IDR>...</IDR>"
}

Usage:
  python convert_idr_xml.py input.xml --out out_dir
  python convert_idr_xml.py /path/to/folder --out out_dir

Notes:
- Handles UTF-8 / UTF-16 XML.
- If input is a folder, processes all *.xml recursively.
- Output filename pattern: <PICNO or input-stem>.json
"""
from __future__ import annotations
import sys, re, json, html, argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

@dataclass
class ScanImageEntry:
    img_type: str
    path: str
    entry_id: str
    operate_time: str

@dataclass
class SiigBlock:
    id: str
    type: str
    operation_time: str
    scanimgs: List[ScanImageEntry]
    element: ET.Element

def strip_ns(xml_str: str) -> str:
    """Remove XML namespace declarations for simpler XPath."""
    return re.sub(r'\sxmlns(:\w+)?="[^"]+"', '', xml_str)

def detect_and_read(path: Path) -> str:
    """Read file trying UTF-8, then UTF-16."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        return path.read_text(encoding="utf-16")

def tag_name(elem: ET.Element) -> str:
    """Return tag name without namespace."""
    return elem.tag.split('}', 1)[-1]

def append_line(lines: List[str], indent: int, content: str) -> None:
    """Append XML line with 2-space indentation."""
    lines.append("  " * indent + content)

def format_simple_tag(tag: str, value: str, indent: int) -> str:
    """Format simple <tag>value</tag> with escaping."""
    text = (value or "").strip()
    prefix = "  " * indent
    if text:
        return f"{prefix}<{tag}>{escape(text)}</{tag}>"
    return f"{prefix}<{tag}/>"

def render_simple_children(element: ET.Element, indent: int) -> List[str]:
    """Render child elements preserving order."""
    lines: List[str] = []
    for child in element:
        name = tag_name(child)
        if list(child):
            append_line(lines, indent, f"<{name}>")
            lines.extend(render_simple_children(child, indent + 1))
            append_line(lines, indent, f"</{name}>")
        else:
            lines.append(format_simple_tag(name, child.text or "", indent))
    return lines

def render_inputinfo(siig_elem: ET.Element, indent: int) -> List[str]:
    """Render inputinfo payload matching expected structure."""
    lines: List[str] = []
    append_line(lines, indent, "<inputinfo>")

    general = siig_elem.find('./IDR_SII_INPUTINFO_GENERAL')
    if general is not None:
        append_line(lines, indent + 1, "<general>")
        container = general.find('./IDR_SII_INPUTINFO_CONTAINER')
        if container is not None:
            append_line(lines, indent + 2, "<container>")
            lines.extend(render_simple_children(container, indent + 3))
            append_line(lines, indent + 2, "</container>")
        for child in general:
            if tag_name(child) == "IDR_SII_INPUTINFO_CONTAINER":
                continue
            if list(child):
                append_line(lines, indent + 2, f"<{tag_name(child)}>")
                lines.extend(render_simple_children(child, indent + 3))
                append_line(lines, indent + 2, f"</{tag_name(child)}>")
            else:
                lines.append(format_simple_tag(tag_name(child), child.text or "", indent + 2))
        append_line(lines, indent + 1, "</general>")

    doc_control = siig_elem.find('./IDR_SII_INPUTINFO_DOCUMENT_CONTROL')
    doc_message = siig_elem.find('./IDR_SII_INPUTINFO_DOCUMENT_MESSAGE')
    if doc_control is not None or doc_message is not None:
        append_line(lines, indent + 1, "<document>")
        if doc_control is not None:
            append_line(lines, indent + 2, "<control>")
            lines.extend(render_simple_children(doc_control, indent + 3))
            append_line(lines, indent + 2, "</control>")
        if doc_message is not None:
            append_line(lines, indent + 2, "<message>")
            lines.extend(render_simple_children(doc_message, indent + 3))
            append_line(lines, indent + 2, "</message>")
        append_line(lines, indent + 1, "</document>")

    official = siig_elem.find('./IDR_SII_INPUTINFO_OFFICIAL')
    if official is not None:
        append_line(lines, indent + 1, "<official>")
        lines.extend(render_simple_children(official, indent + 2))
        append_line(lines, indent + 1, "</official>")

    planning = siig_elem.find('./IDR_SII_INPUTINFO_PLANNING')
    if planning is not None:
        append_line(lines, indent + 1, "<planning>")
        lines.extend(render_simple_children(planning, indent + 2))
        append_line(lines, indent + 1, "</planning>")

    append_line(lines, indent, "</inputinfo>")
    return lines

def collect_siig_blocks(idr_img: ET.Element) -> List[SiigBlock]:
    """
    Collect IDR_SIIG blocks in order, bundling optional scan image details.
    """
    blocks: List[SiigBlock] = []
    check_unit = idr_img.find('./IDR_CHECK_UNIT')
    if check_unit is None:
        return blocks

    for siig in check_unit.findall('./IDR_SIIG'):
        sid = (siig.findtext('ID') or "").strip().strip("{}")
        stype = (siig.findtext('TYPE') or "").strip()
        optime = (siig.findtext('OPERATIONTIME') or "").strip()
        scans: List[ScanImageEntry] = []
        for child in siig:
            name = tag_name(child)
            if name not in ("IDR_SII_SCANIMG", "SCANIMG"):
                continue
            scans.append(
                ScanImageEntry(
                    img_type=(child.findtext('TYPE') or "").strip(),
                    path=(child.findtext('PATH') or "").strip(),
                    entry_id=(child.findtext('ENTRY_ID') or "").strip(),
                    operate_time=(child.findtext('OPERATETIME') or "").strip(),
                )
            )
        blocks.append(SiigBlock(id=sid, type=stype, operation_time=optime, scanimgs=scans, element=siig))
    return blocks

def ensure_ftp_path(path: str) -> str:
    p = path.strip()
    if not p.startswith('/'):
        p = '/' + p
    return p if p.startswith('/import/') else f"/import{p}"

def device_no_from(picno: str, path: str) -> str:
    segment = ''
    if path:
        parts = path.strip('/').split('/')
        segment = parts[0] if parts else ''
    if segment:
        return segment
    m = re.match(r'([A-Z0-9]{8,10})', picno or '')
    if m:
        return m.group(1)
    return ''

def build_image_msg(root: ET.Element) -> Tuple[str, str]:
    idr_img = root.find('./IDR_IMAGE')
    if idr_img is None:
        raise ValueError("Missing <IDR_IMAGE>")

    picno = (idr_img.findtext('PICNO') or '').strip()
    path = (idr_img.findtext('PATH') or '').strip()
    scantime = (idr_img.findtext('SCANTIME') or '').strip()

    imgtype_escaped = (idr_img.findtext('IMGTYPE') or '').strip()
    imgtype_unescaped = html.unescape(imgtype_escaped)

    group_id = None
    try:
        img_root = ET.fromstring(strip_ns(imgtype_unescaped))
        dias = img_root.find('.//DIAS2RIP_XML')
        if dias is not None:
            group_id = (dias.findtext('ImageFolder') or '').strip()
    except ET.ParseError:
        pass

    check_unit = idr_img.find('./IDR_CHECK_UNIT')
    check_id = image_id = unit_id = check_in_time = ''
    if check_unit is not None:
        check_id = (check_unit.findtext('ID') or '').strip().strip('{}')
        image_id = (check_unit.findtext('IMAGEID') or '').strip().strip('{}')
        unit_id = (check_unit.findtext('UNITID') or '').strip()
        check_in_time = (check_unit.findtext('CHECKINTIME') or '').strip()
    if not image_id:
        image_id = (idr_img.findtext('ID') or '').strip().strip('{}')
    if not unit_id:
        unit_id = picno

    conclusion_is_false = any(
        ((c.text or '').strip().lower() == 'no suspect') for c in idr_img.findall('.//IDR_CONCLUSION/TYPE')
    )

    siigs = collect_siig_blocks(idr_img)

    lines: List[str] = []
    append_line(lines, 0, "<IDR>")
    append_line(lines, 1, "<IDR_IMAGE>")
    if image_id:
        append_line(lines, 2, f"<ID>{escape(image_id)}</ID>")
    if picno:
        append_line(lines, 2, f"<PICNO>{escape(picno)}</PICNO>")
    if path:
        append_line(lines, 2, f"<PATH>{escape(path)}</PATH>")
    if scantime:
        append_line(lines, 2, f"<SCANTIME>{escape(scantime)}</SCANTIME>")
    append_line(lines, 2, f"<IMGTYPE>{imgtype_unescaped}</IMGTYPE>")
    append_line(lines, 2, "<IDR_CHECK_UNIT>")
    if check_id:
        append_line(lines, 3, f"<ID>{escape(check_id)}</ID>")
    if image_id:
        append_line(lines, 3, f"<IMAGEID>{escape(image_id)}</IMAGEID>")
    if unit_id:
        append_line(lines, 3, f"<UNITID>{escape(unit_id)}</UNITID>")
    if check_in_time:
        append_line(lines, 3, f"<CHECKINTIME>{escape(check_in_time)}</CHECKINTIME>")

    for block in siigs:
        append_line(lines, 3, "<IDR_SIIG>")
        if block.id:
            append_line(lines, 4, f"<ID>{escape(block.id)}</ID>")
        if block.type:
            append_line(lines, 4, f"<TYPE>{escape(block.type)}</TYPE>")
        if block.operation_time:
            append_line(lines, 4, f"<OPERATIONTIME>{escape(block.operation_time)}</OPERATIONTIME>")
        block_type = (block.type or '').lower()
        if block_type == "scanimg" and block.scanimgs:
            for scan in block.scanimgs:
                append_line(lines, 4, "<SCANIMG>")
                if scan.img_type:
                    append_line(lines, 5, f"<TYPE>{escape(scan.img_type)}</TYPE>")
                if scan.path:
                    append_line(lines, 5, f"<PATH>{escape(scan.path)}</PATH>")
                if scan.entry_id:
                    append_line(lines, 5, f"<ENTRY_ID>{escape(scan.entry_id)}</ENTRY_ID>")
                if scan.operate_time:
                    append_line(lines, 5, f"<OPERATETIME>{escape(scan.operate_time)}</OPERATETIME>")
                append_line(lines, 4, "</SCANIMG>")
        elif block_type == "inputinfo":
            lines.extend(render_inputinfo(block.element, indent=4))
        append_line(lines, 3, "</IDR_SIIG>")

    append_line(lines, 2, "</IDR_CHECK_UNIT>")
    append_line(lines, 1, "</IDR_IMAGE>")

    device_no = device_no_from(picno, path)
    if device_no:
        append_line(lines, 1, f"<DEVICE_NO>{escape(device_no)}</DEVICE_NO>")
    append_line(lines, 1, "<IMAGE_TYPE>img</IMAGE_TYPE>")
    append_line(lines, 1, "<CONCLUSION>false</CONCLUSION>" if conclusion_is_false else "<CONCLUSION>true</CONCLUSION>")
    if group_id:
        append_line(lines, 1, f"<GROUP_ID>{escape(group_id)}</GROUP_ID>")
    append_line(lines, 1, "<gps_info><longitude></longitude><latitude></latitude></gps_info>")
    append_line(lines, 1, "<GROUP_INDEX>1-1</GROUP_INDEX>")
    append_line(lines, 1, "<ISWISCAN>0</ISWISCAN>")
    append_line(lines, 0, "</IDR>")

    image_msg_xml = "\n".join(lines)
    ftp_path = ensure_ftp_path(path) if path else ""
    return ftp_path, image_msg_xml

def process_one(xml_path: Path, out_dir: Path) -> Optional[Path]:
    try:
        xml_text = detect_and_read(xml_path)
        root = ET.fromstring(strip_ns(xml_text))
        ftp_path, image_msg_xml = build_image_msg(root)

        # Determine output name: prefer PICNO
        idr_img = root.find('./IDR_IMAGE')
        picno = (idr_img.findtext('PICNO') or '').strip() if idr_img is not None else ''

        out_name = (picno if picno else xml_path.stem) + ".json"
        out_path = out_dir / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {"ftp_path": ftp_path, "image_msg": image_msg_xml}
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Wrote: {out_path}")
        return out_path
    except Exception as e:
        print(f"ERROR {xml_path}: {e}", file=sys.stderr)
        return None

def iter_xmls(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    return list(path.rglob("*.xml"))

def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Convert IDR XML -> JSON (ftp_path, image_msg).")
    ap.add_argument("input", help="XML file or folder containing XMLs")
    ap.add_argument("--out", default="out", help="Output folder (default: ./out)")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not in_path.exists():
        print(f"Input does not exist: {in_path}", file=sys.stderr)
        return 2

    xml_files = iter_xmls(in_path)
    if not xml_files:
        print("No XML files found.", file=sys.stderr)
        return 1

    ok = 0
    for xp in xml_files:
        if process_one(xp, out_dir):
            ok += 1

    print(f"Done. Converted {ok}/{len(xml_files)} file(s). Output dir: {out_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
