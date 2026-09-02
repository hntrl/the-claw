import unittest

from realtime_service.claw_tools import ClawToolbox


class FakeController:
    async def move_axis(self, axis, degrees):
        return {"ok": True, "command": f"move_{axis}", "degrees": degrees}

    async def lower_claw(self, degrees):
        return {"ok": True, "command": "lower", "degrees": degrees}

    async def open_claw(self, angle):
        return {"ok": True, "command": "open", "angle": angle}

    async def close_claw(self, angle):
        return {"ok": True, "command": "close", "angle": angle}


class ToolAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_existing_tool_names_have_descriptions(self):
        events = []

        async def emit(event):
            events.append(event)

        toolbox = ClawToolbox(FakeController(), emit, lambda: True)
        tools = toolbox.attachments()
        self.assertEqual(
            [tool.name for tool in tools],
            [
                "move_axis",
                "open_claw",
                "lower_claw",
                "raise_claw",
                "close_claw",
                "home_z",
                "home",
                "get_state",
                "halt",
                "reset_emergency",
                "set_expression",
            ],
        )
        self.assertTrue(all(tool.description for tool in tools))
        by_name = {tool.name: tool.params_json_schema for tool in tools}
        self.assertEqual(by_name["move_axis"]["required"], ["axis", "degrees"])
        self.assertEqual(
            by_name["move_axis"]["properties"]["axis"]["enum"], ["x", "y", "z"]
        )
        self.assertEqual(by_name["set_expression"]["required"], ["mood"])
        self.assertEqual(
            by_name["lower_claw"]["properties"]["degrees"]["anyOf"][0]["minimum"],
            0,
        )
        self.assertEqual(
            by_name["set_expression"]["properties"]["mood"]["enum"],
            [
                "calm",
                "blink",
                "wink",
                "suspicious",
                "excited",
                "confused",
                "thinking",
                "nervous",
                "stressed",
                "disappointed",
                "surprised",
                "love",
            ],
        )
        for name in (
            "open_claw",
            "raise_claw",
            "close_claw",
            "home_z",
            "home",
            "get_state",
            "halt",
            "reset_emergency",
        ):
            self.assertEqual(by_name[name]["properties"], {})

    async def test_move_axis_emits_motion_before_controller_result(self):
        events = []

        async def emit(event):
            events.append(event)

        toolbox = ClawToolbox(FakeController(), emit, lambda: True)
        result = await toolbox.move_axis("y", -12)
        self.assertTrue(result["ok"])
        self.assertEqual(events[0], {"type": "state", "state": "moving"})
        self.assertEqual(events[1]["direction"], "left")
        self.assertEqual(events[2]["step"], "claw_execute")

    async def test_interrupted_turn_does_not_issue_hardware_command(self):
        emitted = []

        async def emit(event):
            emitted.append(event)

        toolbox = ClawToolbox(FakeController(), emit, lambda: False)
        result = await toolbox.open_claw()
        self.assertEqual(result, {"ok": False, "error": "turn_interrupted"})
        self.assertEqual(emitted, [])
