import re
import allure
from playwright.sync_api import Page


class AdminApplicationPage:
    """后台管理系统-申请单管理页面对象（Arco Design Vue）。

    注意：后台系统与被测前端相互独立，不继承 BasePage。
    页面 DOM 参考：/Users/tanzsongsen/Documents/申请单管理页dom.md
    """

    def __init__(self, page: Page):
        self.page = page

    @allure.step("进入申请单管理页面")
    def goto_application_page(self, timeout: int = 20000):
        """登录后确保进入申请单管理页。

        策略：
        1. 等待页面加载完成
        2. 若已在申请单管理页（通过手机号搜索框判断），直接返回
        3. 优先直接访问 /apply/apply-management 路径（最可靠）
        4. 兜底：点击侧边栏 .arco-menu-item 菜单项
        """
        self.page.wait_for_load_state("networkidle", timeout=timeout)
        self.page.wait_for_timeout(1500)

        # 检查是否已经在申请单管理页
        if self._has_phone_search_box():
            allure.attach("已在申请单管理页", name="页面定位")
            return

        # 优先：直接访问已知路径（最可靠）
        cur = self.page.url
        base_m = re.match(r'^(https?://[^/]+)', cur)
        if base_m:
            base = base_m.group(1)
            for path in ["/apply/apply-management", "/apply/apply", "/application"]:
                try:
                    self.page.goto(f"{base}{path}", timeout=15000)
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                    self.page.wait_for_timeout(1500)
                    if self._has_phone_search_box():
                        allure.attach(f"通过访问路径'{path}'进入申请单管理页", name="页面定位")
                        return
                except Exception:
                    pass

        # 兜底：点击侧边栏 .arco-menu-item（避免 :text-is 匹配到面包屑/Tab）
        for menu_text in ["申请单管理", "申请单", "订单管理", "申请管理"]:
            try:
                # 使用 .arco-menu-item 容器定位，避免匹配到面包屑/Tab
                menu = self.page.locator(f'.arco-menu-item:has-text("{menu_text}")').first
                if menu.is_visible(timeout=2000):
                    menu.click()
                    self.page.wait_for_timeout(2000)
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                    if self._has_phone_search_box():
                        allure.attach(f"通过点击菜单'{menu_text}'进入申请单管理页", name="页面定位")
                        return
            except Exception:
                pass

        allure.attach(f"⚠ 未明确进入申请单管理页，当前URL: {self.page.url}", name="页面定位警告")

    def _has_phone_search_box(self) -> bool:
        """检查是否存在手机号搜索框（#phone 容器内含 arco-input）"""
        try:
            return self.page.locator('#phone input.arco-input').count() > 0
        except Exception:
            return False

    @allure.step("在手机号搜索框输入: {phone}")
    def input_phone(self, phone: str):
        """在顶部手机号搜索框输入手机号。

        选择器：#phone 容器内的 input.arco-input（placeholder="请输入手机号"）
        """
        # 优先用 id 定位（最稳定）
        try:
            inp = self.page.locator('#phone input.arco-input').first
            if inp.is_visible(timeout=5000):
                inp.click()
                inp.fill("")
                inp.fill(phone)
                allure.attach(f"已通过 #phone 定位输入手机号: {phone}", name="手机号输入")
                return
        except Exception:
            pass

        # 兜底：通过 placeholder 定位
        try:
            inp = self.page.locator('input[placeholder="请输入手机号"]').first
            if inp.is_visible(timeout=3000):
                inp.click()
                inp.fill(phone)
                allure.attach(f"已通过 placeholder 定位输入手机号: {phone}", name="手机号输入")
                return
        except Exception:
            pass

        # 最终兜底：通过 label 文本定位
        try:
            self.page.evaluate("""(args) => {
                const labels = document.querySelectorAll('.arco-form-item-label');
                for (const label of labels) {
                    if (label.textContent.includes('手机号')) {
                        const item = label.closest('.arco-form-item');
                        if (item) {
                            const inp = item.querySelector('input.arco-input');
                            if (inp) {
                                // 触发 React/Vue 受控组件的 input 事件
                                const proto = HTMLInputElement.prototype;
                                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                                setter.call(inp, args.phone);
                                inp.dispatchEvent(new Event('input', {bubbles: true}));
                                inp.dispatchEvent(new Event('change', {bubbles: true}));
                                return true;
                            }
                        }
                    }
                }
                return false;
            }""", phone)
            allure.attach(f"已通过 label 兜底输入手机号: {phone}", name="手机号输入")
        except Exception as e:
            allure.attach(f"输入手机号失败: {e}", name="手机号输入错误")

    @allure.step("获取手机号搜索框的值")
    def get_phone_value(self) -> str:
        try:
            return self.page.locator('#phone input.arco-input').first.input_value(timeout=3000)
        except Exception:
            try:
                return self.page.locator('input[placeholder="请输入手机号"]').first.input_value(timeout=3000)
            except Exception:
                return ""

    @allure.step("点击搜索按钮")
    def click_search(self):
        """点击搜索按钮。

        选择器：button.arco-btn-primary 且文本包含"搜索"
        """
        # 优先按 primary 按钮文本定位
        for sel in [
            'button.arco-btn-primary:has-text("搜索")',
            'button:has-text("搜索")',
            'button.arco-btn-primary',
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    allure.attach(f"已通过选择器'{sel}'点击搜索", name="搜索点击")
                    # 等待表格刷新
                    self.page.wait_for_timeout(2000)
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    self.page.wait_for_timeout(1000)
                    return
            except Exception:
                pass

        # 兜底：JS 点击
        try:
            clicked = self.page.evaluate("""() => {
                const buttons = document.querySelectorAll('button.arco-btn-primary');
                for (const btn of buttons) {
                    if (btn.textContent.includes('搜索')) {
                        btn.click();
                        return true;
                    }
                }
                // 退而求其次：任意含"搜索"文本的按钮
                const allBtns = document.querySelectorAll('button');
                for (const btn of allBtns) {
                    if (btn.textContent.trim() === '搜索') {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            allure.attach(f"JS 兜底点击搜索: {clicked}", name="搜索点击")
            self.page.wait_for_timeout(2000)
        except Exception as e:
            allure.attach(f"点击搜索失败: {e}", name="搜索点击错误")

    @allure.step("点击重置按钮")
    def click_reset(self):
        """点击重置按钮清空搜索条件"""
        try:
            btn = self.page.locator('button.arco-btn-secondary:has-text("重置")').first
            if btn.is_visible(timeout=3000):
                btn.click()
                self.page.wait_for_timeout(1500)
        except Exception:
            pass

    @allure.step("获取表格数据行数")
    def get_row_count(self) -> int:
        """获取 arco-table 表格中数据行数（tbody 内 tr.arco-table-tr 的数量）"""
        try:
            count = self.page.locator('tbody tr.arco-table-tr').count()
            allure.attach(f"表格数据行数: {count}", name="行数统计")
            return count
        except Exception as e:
            allure.attach(f"获取行数失败: {e}", name="行数统计错误")
            return 0

    @allure.step("获取分页总数文本")
    def get_pagination_total_text(self) -> str:
        """获取分页栏的"共 X 条"文本"""
        try:
            el = self.page.locator('.arco-pagination-total').first
            if el.is_visible(timeout=3000):
                txt = el.inner_text()
                allure.attach(f"分页总数文本: {txt}", name="分页信息")
                return txt
        except Exception:
            pass
        return ""

    @allure.step("获取分页总数数字")
    def get_total_count(self) -> int:
        """从"共 X 条"文本中提取数字"""
        txt = self.get_pagination_total_text()
        if txt:
            m = re.search(r'(\d+)', txt)
            if m:
                return int(m.group(1))
        # 兜底：用表格行数
        return self.get_row_count()

    @allure.step("检查是否有空状态提示")
    def has_empty_state(self) -> bool:
        """检查表格是否显示空状态（暂无数据）"""
        try:
            # arco-table 空状态可能有 .arco-empty 类或"暂无数据"文本
            if self.page.locator('.arco-empty').count() > 0:
                return True
            body_text = self.page.inner_text('body', timeout=3000)
            if '暂无数据' in body_text or 'No Data' in body_text:
                return True
        except Exception:
            pass
        return False

    @allure.step("断言列表有数据")
    def has_data(self) -> bool:
        """断言列表中是否有数据（行数 > 0 或分页总数 > 0）"""
        row_count = self.get_row_count()
        if row_count > 0:
            return True
        total = self.get_total_count()
        if total > 0:
            return True
        return not self.has_empty_state()

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    @allure.step("获取表格第一行的手机号和车牌号")
    def get_first_row_phone_and_car(self) -> dict:
        """获取表格第一行的手机号和车牌号，用于前后台数据一致性断言。

        表格列顺序（参考DOM）：
        申请单号、申请时间、用户最后操作时间、客户名称、手机号、车牌号、
        网申状态、审批状态、所在省份、产品、当前节点额度、一级申请渠道、
        二级申请渠道、三级申请渠道、注册渠道、申请渠道、操作

        Returns:
            {"phone": "13800123456", "carNumber": "京A12345"} 或空字典
        """
        try:
            row = self.page.locator('tbody tr.arco-table-tr').first
            if not row.is_visible(timeout=5000):
                return {}

            cells = row.locator('td')
            count = cells.count()
            if count < 6:
                return {}

            # 手机号是第5列（index 4），车牌号是第6列（index 5）
            phone_text = cells.nth(4).inner_text(timeout=3000).strip().strip('"')
            car_text = cells.nth(5).inner_text(timeout=3000).strip().strip('"')

            result = {"phone": phone_text, "carNumber": car_text}
            allure.attach(
                f"后台表格第一行 - 手机号: {phone_text}, 车牌号: {car_text}",
                name="后台表格数据",
                attachment_type=allure.attachment_type.TEXT,
            )
            return result
        except Exception as e:
            allure.attach(f"获取表格行数据失败: {e}", name="表格数据获取错误")
            return {}

    @allure.step("获取表格所有行的手机号列表")
    def get_all_phone_numbers(self) -> list:
        """获取表格所有行的手机号列表，用于验证手机号精确匹配。"""
        try:
            rows = self.page.locator('tbody tr.arco-table-tr')
            count = rows.count()
            phones = []
            for i in range(count):
                cells = rows.nth(i).locator('td')
                if cells.count() >= 5:
                    phone = cells.nth(4).inner_text(timeout=2000).strip().strip('"')
                    phones.append(phone)
            allure.attach(f"表格手机号列表: {phones}", name="手机号列表")
            return phones
        except Exception as e:
            allure.attach(f"获取手机号列表失败: {e}", name="手机号列表错误")
            return []
