class UIActions:

    def __init__(self, page):
        self.page = page

    def open(self, url):
        self.page.goto(url)

    def click(self, selector):
        self.page.click(selector)

    def input(self, selector, value):
        self.page.fill(selector, value)
