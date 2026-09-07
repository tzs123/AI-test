from playwright.sync_api import Page

class StepExecutor:

    def __init__(self, page: Page):
        self.page = page

    def execute(self, step):

        action = step["action"]

        if action == "open":
            self.page.goto(step["url"])

        elif action == "input":
            self.page.fill(f"[name={step['locator']}]", step["value"])

        elif action == "click":
            self.page.click(f"[name={step['locator']}]")

        elif action == "input_plate":
            # ⚠️ 你的车牌是组件式输入（重点）
            value = step["value"]
            for i, char in enumerate(value):
                self.page.click(f".plate-input span:nth-child({i+1})")
                self.page.keyboard.press(char)
