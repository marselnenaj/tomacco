"""screen.tree's compact and detail modes through tomaccoctl and MCP on the offline
fixture runtime, against the default document-order tree on the same hierarchy: a
long header above a search field, a cart with duplicate Delete buttons, a disabled
and a stateful control, the Pay now button at the bottom, a hidden and an off-screen
node and right-to-left text. Every device helper is trapped. The sizes it prints are
fixture data, not a measurement of any device, model or token count."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_fixture import Isolated, render  # noqa: E402
from robust_support import Session  # noqa: E402

W, H = 360, 800
GREETING = "مرحبا بالعالم \U0001f44b שלום"


def node(cls, attributes="", children=()):
    enabled = "" if "enabled=" in attributes else 'enabled="true" '
    return f'<node class="{cls}" package="dev.example.shop" {enabled}{attributes}>{"".join(children)}</node>'


def text(words, box):
    return node("android.widget.TextView", f'text="{words}" bounds="[{box[0]},{box[1]}][{box[2]},{box[3]}]"')


def button(words, ident, box, extra=""):
    return node("android.widget.Button", f'text="{words}" resource-id="dev.example.shop:id/{ident}" '
                f'bounds="[{box[0]},{box[1]}][{box[2]},{box[3]}]" clickable="true" {extra}')


def shop_xml():
    header = [text(f"Headline {i}: a long sentence of the news ticker that fills the header of the shop with words",
                   (4, 4 + i * 9, 356, 12 + i * 9)) for i in range(30)]
    header += [button(f"Category {i}", f"category_{i}", (4, 280 + i * 10, 356, 289 + i * 10)) for i in range(24)]

    def row(name, top, inner):
        return node("android.widget.LinearLayout", f'bounds="[0,{top}][360,{top + 40}]" clickable="true"',
                    [text(name, (10, top + 5, 170, top + 35))] + inner)
    cart = node("androidx.recyclerview.widget.RecyclerView",
                'resource-id="dev.example.shop:id/cart_list" bounds="[0,590][360,710]" scrollable="true"', [
                    row("Apple", 590, [text("€1.20", (200, 595, 350, 625))]),
                    row("Pear", 630, [button("Delete", "delete", (270, 635, 350, 665))]),
                    row("Plum", 670, [button("Delete", "delete", (270, 675, 350, 705))])])
    body = [node("android.widget.LinearLayout", 'bounds="[0,0][360,540]"', header),
            node("android.widget.EditText", 'text="Search products" resource-id="dev.example.shop:id/search" '
                 'bounds="[10,550][350,585]" clickable="true" focusable="true"'),
            cart,
            node("android.widget.Switch", 'text="Gift wrap" resource-id="dev.example.shop:id/gift" bounds="[10,715][170,735]" '
                 'checkable="true" checked="false" clickable="true"'),
            button("Coupon", "coupon", (190, 715, 350, 735), 'enabled="false"'),
            button("Pay now", "pay", (10, 740, 350, 778)),
            text("Hidden promo", (0, 0, 0, 0)),
            button("Below fold", "below", (10, 820, 350, 860)),
            node("android.widget.LinearLayout", 'resource-id="dev.example.shop:id/greeting" bounds="[0,780][360,800]" clickable="true"',
                 [text(GREETING, (10, 782, 350, 798))])]
    return ("<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">"
            + node("android.widget.FrameLayout", f'bounds="[0,0][{W},{H}]"', body) + "</hierarchy>")


def write_shop(root, name="shop"):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    render(directory / "shop.png", (W, H), "Shop")
    render(directory / "paid.png", (W, H), "Paid", shade=(210, 230, 210))
    (directory / "shop.xml").write_text(shop_xml(), encoding="utf-8")
    (directory / "paid.xml").write_text("<hierarchy rotation=\"0\">" + node("android.widget.FrameLayout", f'bounds="[0,0][{W},{H}]"',
                                        [text("Thank you", (10, 100, 350, 140))]) + "</hierarchy>", encoding="utf-8")
    app = {"bundle_id": "dev.example.shop", "name": "Shop", "home_screen": False}
    scenario = {"initial": "shop", "latency_ms": 0, "states": {
        "shop": {"frame": "shop.png", "hierarchy": "shop.xml", "app": app, "keyboard": False, "focused": False,
                 "taps": [{"left": 10, "top": 740, "right": 350, "bottom": 778, "to": "paid", "after_ms": 0}]},
        "paid": {"frame": "paid.png", "hierarchy": "paid.xml", "app": app, "keyboard": False, "focused": False,
                 "key_to": {"back": "shop"}}}}
    (directory / "scenario.json").write_text(json.dumps(scenario, indent=1))


def marks_in(text):
    return set(re.findall(r"(?:^|\s)#(\d+)(?=\s|$)", text, re.M))


class ObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Isolated(prefix="observe-")
        write_shop(cls.box.fixtures)
        cls.session = cls.box.result("runtime.start", {"platform": "android", "device": "fixture:shop"})["session"]

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def setUp(self):
        state = self.box.result("runtime.observe", {"session": self.session})["fixture"]["state"]
        if state != "shop":
            self.box.result("runtime.input", {"session": self.session, "action": "key", "key": "back"})

    def tearDown(self):
        self.assertEqual(self.box.trapped(), "")

    def tree(self, **arguments):
        return self.box.result("screen.tree", {"device": self.session, **arguments})

    def test_the_default_tree_is_unchanged_and_compact_keeps_the_bottom_control_tappable(self):
        default = self.tree(max_chars=1500)
        self.assertEqual(default["mode"], "tree")
        self.assertEqual(default["text"], self.tree(max_chars=1500, mode="tree")["text"])
        self.assertTrue(default["truncated"])
        self.assertNotIn("Pay now", default["text"], "the document-order prefix loses it")
        self.assertTrue(default["text"].endswith("more nodes not shown; raise max_chars\n"))
        compact = self.tree(max_chars=1500, mode="compact")
        self.assertLessEqual(compact["chars"], 1500)
        self.assertEqual(compact["chars"], len(compact["text"]))
        self.assertEqual(compact["bytes"], len(compact["text"].encode()))
        pay = next(line for line in compact["text"].splitlines() if '"Pay now"' in line)
        mark = int(re.search(r"#(\d+)", pay).group(1))
        self.assertRegex(pay, r'^n\d+ Button "Pay now" id=pay #\d+ \(10,740,350,778\)$')
        self.assertGreater(compact["omitted"], 0)
        self.assertEqual(compact["omitted"] + compact["shown"], compact["nodes"])
        # The marks the compact tree printed are the marks screen.tap checks now.
        tapped = self.box.result("screen.tap", {"device": self.session, "mark": mark})
        self.assertEqual(tapped["element"]["text"], "Pay now")
        state = self.box.result("runtime.observe", {"session": self.session})["fixture"]["state"]
        self.assertEqual(state, "paid")

    def test_hidden_details_are_inspected_by_reference_and_never_on_another_screen(self):
        compact = self.tree(max_chars=1500, mode="compact")
        digest = compact["digest"]
        self.assertRegex(digest, r"^[0-9a-f]{16}$")
        omitted = compact["omitted_refs"]
        detail = self.tree(mode="detail", refs=omitted[:3], digest=digest)
        self.assertEqual([r["ref"] for r in detail["records"]], omitted[:3])
        self.assertTrue(all("bounds" in r and "resource_id" in r and "enabled" in r for r in detail["records"]))
        # The folded row names its sources; each source names the row.
        row = re.search(r'(n\d+) LinearLayout label="Apple \| €1.20" from=(n\d+),(n\d+)', self.tree(max_chars=100000, mode="compact")["text"])
        self.assertIsNotNone(row)
        records = self.tree(mode="detail", refs=[row.group(1), row.group(2)], digest=digest)["records"]
        self.assertEqual(records[0]["label_sources"], [row.group(2), row.group(3)])
        self.assertEqual(records[0]["text"], "", "the row's own text, a selector, is unchanged")
        self.assertEqual((records[1]["text"], records[1]["folded_into"]), ("Apple", row.group(1)))
        # Wrong requests are refused before anything else.
        for arguments, code in [({"mode": "detail"}, "invalid_argument"), ({"refs": ["n1"]}, "invalid_argument"),
                                ({"mode": "detail", "refs": ["n99999"]}, "invalid_argument"),
                                ({"mode": "tree", "query": "pay"}, "invalid_argument")]:
            self.assertEqual(self.box.error("screen.tree", {"device": self.session, **arguments})["code"], code, arguments)
        # Another screen: the old digest refuses the old references.
        self.box.result("runtime.input", {"session": self.session, "action": "tap", "x": 0.5, "y": 759 / 799})
        error = self.box.error("screen.tree", {"device": self.session, "mode": "detail", "refs": omitted[:1], "digest": digest})
        self.assertEqual(error["code"], "tree_stale")
        self.assertEqual(error["details"]["expected"], digest)

    def test_cli_and_mcp_answer_the_same_records_and_the_sizes_are_measured_alike(self):
        cli = self.tree(max_chars=3000, mode="compact", query="coupon")
        self.assertIn('"Coupon"', cli["text"])
        self.assertIn(GREETING, self.tree(max_chars=100000, mode="compact")["text"])
        mcp = Session(self.box.env)
        try:
            mcp.initialize()
            reply = mcp.request(1, "tools/call", {"name": "screen.tree", "arguments": {
                "device": self.session, "max_chars": 3000, "mode": "compact", "query": "coupon"}}, timeout=60)
            self.assertFalse(reply["result"]["isError"], reply)
            self.assertEqual(reply["result"]["structuredContent"]["text"], cli["text"])
        finally:
            mcp.kill()
        everything = self.tree(max_chars=100000)
        all_marks = marks_in(everything["text"])
        rows = []
        for budget in (1500, 3000, 6000):
            tree = self.tree(max_chars=budget)
            compact = self.tree(max_chars=budget, mode="compact")
            for name, reply in (("tree", tree), ("compact", compact)):
                rows.append({"budget": budget, "mode": name, "chars": reply["chars"], "bytes": len(reply["text"].encode()),
                             "marks_kept": len(marks_in(reply["text"]) & all_marks), "marks_total": len(all_marks),
                             "pay_now": '"Pay now"' in reply["text"], "search_field": "Search products" in reply["text"],
                             "truncated": reply["truncated"]})
            self.assertTrue(rows[-1]["pay_now"] and rows[-1]["search_field"], rows[-1])
            self.assertLessEqual(rows[-1]["chars"], budget)
        print("FIXTURE-MEASUREMENT " + json.dumps(rows, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
