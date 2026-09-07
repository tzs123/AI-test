class HomePage:

    def __init__(self, page):
        self.page = page

    def open(self, url):
        self.page.goto(url)

    def input_phone(self, phone):
        self.page.fill("input[placeholder*='手机号']", phone)

    def input_captcha(self, code):
        self.page.fill("input[placeholder*='验证码']", code)

    def input_car(self, province, city, num):
        self.page.click("text=车牌号")
        self.page.click(f"text={province}")
        self.page.click(f"text={city}")
        for i in num:
            self.page.click(f"text={i}")

    def submit(self):
        self.page.click("text=同意并申请")
