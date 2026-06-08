#!/usr/bin/env python3
"""
Generates an augmented diff from an OSC (osmChange) file

See https://wiki.openstreetmap.org/wiki/Overpass_API/Augmented_Diffs
This is intended to be run before the OSC file is applied to the osmx file.

Usage: augmented_adiff.py OSMX_FILE OSC_FILE
"""

# This script is adapted from https://github.com/bdon/OSMExpress/blob/main/python/examples/augmented_diff.py
#
# Used under the terms of the BSD 2-Clause License, reproduced below
#
# Copyright 2019 Protomaps.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from collections import namedtuple
from datetime import datetime
import copy
import sys
import time
import xml.etree.ElementTree as ET
import osmx

def eprint(*args, **kwargs):
    print(*args, **kwargs, file=sys.stderr)

if len(sys.argv) < 3:
    eprint("Usage: augmented_diff.py OSMX_FILE OSC_FILE")
    exit(1)

start_time = time.time()

# 1st pass:
# populate the collection of actions
# create dictionary from osm_type/osm_id to action
# e.g. node/12345 > Node()
# FIXME: this assumes the changeset only contains a single action for any element,
# but that's not true for replication changesets. Examples:
# - replication/minute/005/998/765.osc.gz (5998765) contains two changes to way 1260417858,
#   made by the same user (TerraTexGeo) in two different changesets (148485991 and 148485994)
#   about 18 seconds apart
# - replication/minute/005/998/712.osc.gz (5998712) contains two changes to way 1260406351
#   which were made in the same changeset (148485294). the changeset created and then
#   modified the way. it was made using StreetComplete.
Action = namedtuple("Action", ["type", "element"])
actions = {}

osc = ET.parse(sys.argv[2]).getroot()
eprint("Warning: Skipping relations because of custom BM code.")
for block in osc:
    for e in block:
        # ----- Skipping relations part BM -----
        if e.tag == "relation":
            continue
        # ----- Skipping relations part BM -----
        action_key = e.tag + "/" + e.get("id")
        # Always ensure we're updating to the latest version of an object for the diff
        if action_key in actions:
            newest_version = int(actions[action_key].element.get("version"))
            e_version = int(e.get("version"))
            if e_version < newest_version:
                eprint(
                    "Found element {}, version {} is less than previously visited version {}".format(
                        action_key, e_version, newest_version
                    )
                )
                continue
        actions[action_key] = Action(block.tag, e)


action_list = [v for k, v in actions.items()]

eprint(f"Pass 1: {time.time() - start_time:.3f}s")


# The augmented diff used to be assembled into one big ElementTree held in memory
# until the very end (every pass operated on the whole tree at once). For diffs
# that touch nodes on long or heavily-shared ways, Pass 4 expands every such way
# fully (old + a deepcopy of new), so the resident tree grew far faster than the
# input .osc — the cause of the production memory blow-up. Instead each <action>
# is now built, transformed, serialized to a string and dropped immediately; the
# strings are sorted and streamed out at the end. Output is byte-for-byte
# identical (see the regression test in the daemon repo). These module-level
# helpers support that per-action finalize step.
results = []
moved_node_ids = []
# ----- Skipping relations part BM -----
# Re-enable together with the other "Skipping relations part BM" blocks below to
# restore relation propagation. changed_way_ids feeds way->relation propagation only.
# changed_way_ids = []
# ----- Skipping relations part BM -----


def _type_rank(tag):
    if tag == "node":
        return 1
    if tag == "way":
        return 2
    return 3


class Bounds:
    def __init__(self):
        self.minx = 180
        self.maxx = -180
        self.miny = 90
        self.maxy = -90

    def add(self, x, y):
        if x < self.minx:
            self.minx = x
        if x > self.maxx:
            self.maxx = x
        if y < self.miny:
            self.miny = y
        if y > self.maxy:
            self.maxy = y

    def elem(self):
        e = ET.Element("bounds")
        e.set("minlat", str(self.miny))
        e.set("minlon", str(self.minx))
        e.set("maxlat", str(self.maxy))
        e.set("maxlon", str(self.maxx))
        return e


def _finalize(a):
    # Applies the former pass 5 (bounding box) and pass 7 (create reshape) to a
    # single fully-built <action>, then serializes it and records (sort_rank,
    # id, xml) so the element can be released. The sort key is taken from the new
    # element BEFORE the create reshape, matching the original sort over the
    # new-element tag/id.
    sort_elem = a[1][0]
    key_rank = _type_rank(sort_elem.tag)
    key_id = int(sort_elem.get("id"))

    # Pass 5: bounding box on the old element only (matches original behavior).
    if len(a[0]) > 0:
        osm_obj = a[0][0]
        nds = osm_obj.findall(".//nd")
        if nds:
            bounds = Bounds()
            for nd in nds:
                bounds.add(float(nd.get("lon")), float(nd.get("lat")))
            osm_obj.insert(0, bounds.elem())

    # Pass 7: a create action is a single OSM element, not <old>/<new> wrappers.
    if a.get("type") == "create":
        elem = a[1][0]
        a[:] = [elem]

    results.append((key_rank, key_id, ET.tostring(a, encoding="unicode")))


env = osmx.Environment(sys.argv[1])
with osmx.Transaction(env) as txn:
    locations = osmx.Locations(txn)
    nodes = osmx.Nodes(txn)
    ways = osmx.Ways(txn)
    relations = osmx.Relations(txn)

    def not_in_db(elem):
        elem_id = int(elem.get("id"))
        if elem.tag == "node":
            return not locations.get(elem_id)
        elif elem.tag == "way":
            return not ways.get(elem_id)
        else:
            return not relations.get(elem_id)

    def get_lat_lon(ref, use_new):
        if use_new and ("node/" + ref in actions):
            node = actions["node/" + ref]
            return (node.element.get("lon"), node.element.get("lat"))
        else:
            ll = locations.get(ref)
            return (str(ll[1]), str(ll[0]))

    def set_old_metadata(elem):
        elem_id = int(elem.get("id"))
        if elem.tag == "node":
            o = nodes.get(elem_id)
        elif elem.tag == "way":
            o = ways.get(elem_id)
        else:
            o = relations.get(elem_id)
        if o:
            with o as o:
                elem.set("version", str(o.metadata.version))
                elem.set("user", str(o.metadata.user))
                elem.set("uid", str(o.metadata.uid))
                # convert to ISO8601 timestamp
                timestamp = o.metadata.timestamp
                formatted = datetime.utcfromtimestamp(timestamp).isoformat()
                elem.set("timestamp", formatted + "Z")
                elem.set("changeset", str(o.metadata.changeset))
        else:
            # tagless nodes
            try:
                version = locations.get(elem_id)[2]
                elem.set("version", str(version))
            except TypeError:
                # If loc is None here, it typically means that a node was created and
                # then deleted within the diff interval. In the future we should
                # remove these operations from the diff entirely.
                eprint("No old loc found for tagless node {}".format(elem_id))
                # elem.set("version", "?")

            # elem.set("user", "?")
            # elem.set("uid", "?")
            # elem.set("timestamp", "?")
            # elem.set("changeset", "?")


    # 3rd pass helpers
    # Augment the created "old" and "new" elements with geometry.
    def augment_nd(nd, use_new):
        ll = get_lat_lon(nd.get("ref"), use_new)
        nd.set("lon", ll[0])
        nd.set("lat", ll[1])

    def augment_member(mem, use_new):
        if mem.get("type") == "way":
            ref = mem.get("ref")
            if use_new and ("way/" + ref in actions):
                way = actions["way/" + ref]
                for child in way.element:
                    if child.tag == "nd":
                        ref = child.get("ref")
                        ll = get_lat_lon(ref, use_new)
                        nd = ET.SubElement(mem, "nd")
                        nd.set("ref", ref)
                        nd.set("lon", ll[0])
                        nd.set("lat", ll[1])
            else:
                with ways.get(ref) as way:
                    for node_id in way.nodes:
                        ref = str(node_id)
                        ll = get_lat_lon(ref, use_new)
                        nd = ET.SubElement(mem, "nd")
                        nd.set("ref", ref)
                        nd.set("lon", ll[0])
                        nd.set("lat", ll[1])
        elif mem.get("type") == "node":
            ll = get_lat_lon(mem.get("ref"), use_new)
            mem.set("lon", ll[0])
            mem.set("lat", ll[1])

    def augment(elem, use_new):
        if len(elem) == 0:
            return
        if elem[0].tag == "way":
            for child in elem[0]:
                if child.tag == "nd":
                    augment_nd(child, use_new)
        elif elem[0].tag == "relation":
            for child in elem[0]:
                if child.tag == "member":
                    augment_member(child, use_new)

    # 2nd pass
    # build a single <action> with old and new sub-elements. Returns the element
    # so the caller can augment/finalize it; the early returns mirror the original
    # loop's `continue` statements (which left a partially-built action in place).
    def build_action(action):
        a = ET.Element("action")
        a.set("type", action.type)
        old = ET.SubElement(a, "old")
        new = ET.SubElement(a, "new")
        if action.type == "create":
            new.append(action.element)
        elif action.type == "delete":
            # TODO: dedupe this with "modify" case below (jake)
            # I copy-pasted this because deleted elements also need to be augmented
            # with tags and nodes (not just metadata) in order to be visualized in OSMCha
            obj_id = action.element.get("id")
            prev_version = ET.SubElement(old, action.element.tag)
            prev_version.set("id", obj_id)
            set_old_metadata(prev_version)

            # logically this goes at the end, but do it first so that we can use
            # 'return' to skip processing below (python doesn't have 'goto out;')
            action.element.set("visible", "false")
            new.append(action.element)

            # FIXME: is this right? goal here is to avoid crashing when handling
            # tagless nodes that were deleted...
            if prev_version.get("version") == None or prev_version.get("version") == "?":
                return a

            if action.element.tag == "node":
                ll = get_lat_lon(obj_id, False)
                prev_version.set("lon", ll[0])
                prev_version.set("lat", ll[1])
                node = nodes.get(obj_id)
                if node:
                    with node as node:
                        it = iter(node.tags)
                        for t in it:
                            tag = ET.SubElement(prev_version, "tag")
                            tag.set("k", t)
                            tag.set("v", next(it))
            elif action.element.tag == "way":
                way = ways.get(obj_id)
                if not way:
                    # TODO: this seems to be happening (e.g. for way 987234331), might
                    # be a bug in osmx expand?
                    return a
                with way as way:
                    for n in way.nodes:
                        node = ET.SubElement(prev_version, "nd")
                        node.set("ref", str(n))
                    it = iter(way.tags)
                    for t in it:
                        tag = ET.SubElement(prev_version, "tag")
                        tag.set("k", t)
                        tag.set("v", next(it))
            else:
                relation = relations.get(obj_id)
                if not relation:
                    # TODO: this guard avoids errors from the 'with' statement
                    # like "AttributeError: __enter__ -:1.1: Document is empty"
                    return a
                with relation as relation:
                    for m in relation.members:
                        member = ET.SubElement(prev_version, "member")
                        member.set("ref", str(m.ref))
                        member.set("role", m.role)
                        member.set("type", str(m.type))
                    it = iter(relation.tags)
                    for t in it:
                        tag = ET.SubElement(prev_version, "tag")
                        tag.set("k", t)
                        tag.set("v", next(it))
        else:
            obj_id = action.element.get("id")
            if not_in_db(action.element):
                # Typically occurs when:
                #  1. TODO: An element is deleted but then restored later,
                #     which should remain a modify operation. This will be difficult
                #     because objects are not retained in OSMX when deleted in OSM.
                #  2. OK: An element was created and then modified within the diff interval
                eprint(
                    "Could not find {0} {1} in db, changing to create".format(
                        action.element.tag, action.element.get("id")
                    )
                )
                new.append(action.element)
                a.set("type", "create")
            else:
                prev_version = ET.SubElement(old, action.element.tag)
                prev_version.set("id", obj_id)
                set_old_metadata(prev_version)
                if action.element.tag == "node":
                    ll = get_lat_lon(obj_id, False)
                    prev_version.set("lon", ll[0])
                    prev_version.set("lat", ll[1])
                    node = nodes.get(obj_id)
                    if node:
                        with node as node:
                            it = iter(node.tags)
                            for t in it:
                                tag = ET.SubElement(prev_version, "tag")
                                tag.set("k", t)
                                tag.set("v", next(it))
                elif action.element.tag == "way":
                    with ways.get(obj_id) as way:
                        for n in way.nodes:
                            node = ET.SubElement(prev_version, "nd")
                            node.set("ref", str(n))
                        it = iter(way.tags)
                        for t in it:
                            tag = ET.SubElement(prev_version, "tag")
                            tag.set("k", t)
                            tag.set("v", next(it))
                else:
                    with relations.get(obj_id) as relation:
                        for m in relation.members:
                            member = ET.SubElement(prev_version, "member")
                            member.set("ref", str(m.ref))
                            member.set("role", m.role)
                            member.set("type", str(m.type))
                        it = iter(relation.tags)
                        for t in it:
                            tag = ET.SubElement(prev_version, "tag")
                            tag.set("k", t)
                            tag.set("v", next(it))
                new.append(action.element)
        return a

    # 2nd + 3rd pass:
    # build each action, augment it, capture nodes that moved (for pass 4), then
    # serialize and drop it.
    pass_2_start_time = time.time()

    for action in action_list:
        a = build_action(action)

        # 4th-pass input: record nodes whose location changed so referencing ways
        # can be pulled in below. Captured here (instead of re-scanning a retained
        # output tree) so the action can be freed right after serialization.
        if a.get("type") == "modify" and len(a[0]) and a[0][0].tag == "node":
            old_loc = (a[0][0].get("lat"), a[0][0].get("lon"))
            new_loc = (a[1][0].get("lat"), a[1][0].get("lon"))
            if old_loc != new_loc:
                moved_node_ids.append(a[0][0].get("id"))
        # ----- Skipping relations part BM -----
        # A way whose node list changed propagates to relations it belongs to.
        # elif a.get("type") == "modify" and len(a[0]) and a[0][0].tag == "way":
        #     old_way = [nd.get("ref") for nd in a[0][0] if nd.tag == "nd"]
        #     new_way = [nd.get("ref") for nd in a[1][0] if nd.tag == "nd"]
        #     if old_way != new_way:
        #         changed_way_ids.append(a[0][0].get("id"))
        # ----- Skipping relations part BM -----

        # 3rd pass: augment old/new geometry
        try:
            augment(a[0], False)
            augment(a[1], True)
        except (TypeError, AttributeError):
            eprint(
                "Changed {0} {1} is incomplete in db".format(
                    a[1][0].tag, a[1][0].get("id")
                )
            )

        _finalize(a)

    eprint(f"Pass 2/3: {time.time() - pass_2_start_time:.3f}s")

    # 4th pass:
    # find changes that propagate to referencing elements:
    # when a node's location changes, that propagates to any ways it belongs to.
    # (relation propagation is disabled per the custom BM code, so only ways.)
    pass_4_start_time = time.time()

    node_way = osmx.NodeWay(txn)
    # ----- Skipping relations part BM -----
    # node_relation = osmx.NodeRelation(txn)
    # way_relation = osmx.WayRelation(txn)
    # ----- Skipping relations part BM -----

    affected_ways = set()
    # affected_relations = set()  # ----- Skipping relations part BM -----
    for node_id in moved_node_ids:
        # ----- Skipping relations part BM -----
        # for rel in node_relation.get(node_id):
        #     if "relation/" + str(rel) not in actions:
        #         affected_relations.add(rel)
        # ----- Skipping relations part BM -----
        for way in node_way.get(node_id):
            if "way/" + str(way) not in actions:
                affected_ways.add(way)
                # ----- Skipping relations part BM -----
                # for rel in way_relation.get(way):
                #     if "relation/" + str(rel) not in actions:
                #         affected_relations.add(rel)
                # ----- Skipping relations part BM -----
    # ----- Skipping relations part BM -----
    # for way_id in changed_way_ids:
    #     for rel in way_relation.get(way_id):
    #         if "relation/" + str(rel) not in actions:
    #             affected_relations.add(rel)
    # ----- Skipping relations part BM -----

    for w in affected_ways:
        a = ET.Element("action")
        a.set("type", "modify")
        old = ET.SubElement(a, "old")
        way_element = ET.SubElement(old, "way")
        way_element.set("id", str(w))
        set_old_metadata(way_element)
        with ways.get(w) as way:
            for n in way.nodes:
                node = ET.SubElement(way_element, "nd")
                node.set("ref", str(n))
            it = iter(way.tags)
            for t in it:
                tag = ET.SubElement(way_element, "tag")
                tag.set("k", t)
                tag.set("v", next(it))

        new = ET.SubElement(a, "new")
        new_elem = copy.deepcopy(way_element)
        new.append(new_elem)
        augment(old, False)
        augment(new, True)
        _finalize(a)

    # ----- Skipping relations part BM -----
    # Emit the relations affected by the propagation above. Re-enable together
    # with all other "Skipping relations part BM" blocks (and use the unfiltered
    # osmx db, which contains relations). Verified output-identical to the
    # original via the relation regression fixture.
    # for r in affected_relations:
    #     old = ET.Element("old")
    #     relation_element = ET.SubElement(old, "relation")
    #     relation_element.set("id", str(r))
    #     set_old_metadata(relation_element)
    #     with relations.get(r) as relation:
    #         for m in relation.members:
    #             member = ET.SubElement(relation_element, "member")
    #             member.set("ref", str(m.ref))
    #             member.set("role", m.role)
    #             member.set("type", str(m.type))
    #         it = iter(relation.tags)
    #         for t in it:
    #             tag = ET.SubElement(relation_element, "tag")
    #             tag.set("k", t)
    #             tag.set("v", next(it))
    #
    #     new_elem = copy.deepcopy(relation_element)
    #     new = ET.Element("new")
    #     new.append(new_elem)
    #     try:
    #         augment(old, False)
    #         augment(new, True)
    #         a = ET.Element("action")
    #         a.set("type", "modify")
    #         a.append(old)
    #         a.append(new)
    #         _finalize(a)
    #     except (TypeError, AttributeError):
    #         eprint("Affected relation {0} is incomplete in db".format(r))
    # ----- Skipping relations part BM -----

    eprint(f"Pass 4: {time.time() - pass_4_start_time:.3f}s")

# 5th-7th passes were applied per-action during _finalize. Here we only order the
# serialized actions and stream them out.
#
# 6th pass: sort by node, way, relation; within each, by increasing ID. The
# original did a stable sort by id then by type, which is equivalent to sorting
# by (type_rank, id).
pass_6_start_time = time.time()
results.sort(key=lambda r: (r[0], r[1]))
eprint(f"Pass 6: {time.time() - pass_6_start_time:.3f}s")

# Build the <osm> wrapper + <note> exactly as before, serialize it once, then
# splice the sorted action strings in before the closing tag. This reproduces
# ElementTree.write(..., encoding="unicode", xml_declaration=True) byte-for-byte
# (the declaration string is the CPython default) without ever holding the whole
# augmented diff in memory at once.
o = ET.Element("osm")
o.set("version", "0.6")
o.set(
    "generator",
    "Overpass API not used, but achavi detects it at the start of string; OSMExpress/python/examples/augmented_diff.py",
)
note = ET.Element("note")
note.text = "The data included in this document is from www.openstreetmap.org. The data is made available under ODbL."
o.append(note)

closing = "</osm>"
header = ET.tostring(o, encoding="unicode")
assert header.endswith(closing), "unexpected <osm> serialization"

write_output_start_time = time.time()

sys.stdout.write("<?xml version='1.0' encoding='utf-8'?>\n")
sys.stdout.write(header[: -len(closing)])
for _, _, action_xml in results:
    sys.stdout.write(action_xml)
sys.stdout.write(closing)
sys.stdout.write("\n")  # tree.write does not write a final newline

end_time = time.time()

eprint(f"Pass 8: {end_time - write_output_start_time:.3f}s")
eprint(f"Generated augmented diff in {end_time - start_time:.3f} seconds")
