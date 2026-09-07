class FillPage:

    def __init__(self, page):
        self.page = page

    def set_region(self, province, city):
        self.page.click("text=所在地区")
        self.page.click(f"text={province}")
        self.page.click(f"text={city}")

    def set_income(self, income):
        self.page.fill("text=年收入 >> input", str(income))

    def submit(self):
        self.page.click("text=完成补充")
