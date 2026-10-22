"""一个有典型问题的示例文件，用来演示 --code 模式。"""


def average(total, count):
    return total / count


def add_item(item, items=[]):
    items.append(item)
    return items


def get_score(text):
    try:
        return int(text)
    except:
        pass


def read_config(path):
    f = open(path)
    return f.read()
