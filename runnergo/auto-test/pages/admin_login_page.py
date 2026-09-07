import allure
from playwright.sync_api import Page


# 后台管理系统地址
ADMIN_BASE_URL = "http://172.16.0.86:5174"


class AdminLoginPage:
    """后台管理系统登录页面对象（Arco Design Vue）。

    注意：后台系统与被测前端相互独立，不继承 BasePage（避免使用被测系统的 BASE_URL）。
    """

    def __init__(self, page: Page, base_url: str = ADMIN_BASE_URL):
        self.page = page
        self.base_url = base_url.rstrip("/")

    @allure.step("打开后台登录页")
    def goto(self, timeout: int = 30000, networkidle_timeout: int = 15000):
        self.page.goto(f"{self.base_url}/login", timeout=timeout)
        self.page.wait_for_load_state("networkidle", timeout=networkidle_timeout)
        self.page.wait_for_timeout(1500)

    @allure.step("输入账号: {username}")
    def input_username(self, username: str):
        # Arco Design 登录表单：input.arco-input，优先按 placeholder 定位
        locators = [
            'input[placeholder*="账号"]',
            'input[placeholder*="用户"]',
            'input[placeholder*="请输入"]',
            'input[type="text"].arco-input',
            'form input.arco-input',
        ]
        for sel in locators:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    el.fill(username)
                    return
            except Exception:
                pass
        # 兜底：取第一个可见的文本输入框
        self.page.locator('input[type="text"]').first.fill(username)

    @allure.step("输入密码")
    def input_password(self, password: str):
        locators = [
            'input[placeholder*="密码"]',
            'input[type="password"].arco-input',
            'input[type="password"]',
        ]
        for sel in locators:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    el.fill(password)
                    return
            except Exception:
                pass

    @allure.step("点击登录按钮")
    def click_login(self):
        # 优先按文本定位登录按钮
        for sel in [
            'button:has-text("登录")',
            'button:has-text("登 录")',
            'button.arco-btn-primary',
            'button[type="submit"]',
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    return
            except Exception:
                pass

    @allure.step("执行登录")
    def login(self, username: str = "admin", password: str = "admin123"):
        """完整登录流程：输入账号密码并点击登录"""
        self.input_username(username)
        self.input_password(password)
        allure.attach(self.page.screenshot(), name="登录前", attachment_type=allure.attachment_type.PNG)
        self.click_login()
        # 等待登录跳转完成
        self.page.wait_for_timeout(2000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        self.page.wait_for_timeout(1500)
        allure.attach(self.page.screenshot(), name="登录后", attachment_type=allure.attachment_type.PNG)

    def get_current_url(self) -> str:
        return self.page.url

    def is_logged_in(self) -> bool:
        """判断是否登录成功（URL 不再包含 /login）"""
        return "/login" not in self.page.url
