# 这边是 Python 的基础知识

## 基础知识

with 的作用：
    本质是进入和退出代码块的时候
        __enter__()
        __exit__()
    异步代码块
        __aenter__()
        __aexit__()
        异步代码块内部发生异常：
            向外抛出异常
            但是如果先发生退出，那么通常向外抛出 TimeoutError

代码参数的内容：
    def func(self, *, name: str):
        if type(name) is not str:
        这边要做一层校验，只是类型注解，不会强制检查内容

## 一些基础函数
lambda _: None  lambda表示一个匿名函数(接收一个参数) _参数名。下划线表示一种约定，这个参数我不用了，只是为了占位
        None 函数体表示函数体返回一个 None 值

arguments.model_dump()
        Pydantic模型对象转换成一个普通的 Python 字典
        class User(BaseModel):        # 只要继承了 BaseModel 对象就是 Pydantic 对象
            name: str
            age: int
        user = User(name="Alice", age=30)
        会得到 {'name': 'Alice', 'age': 30}
        转成 dict 以后，就会方便做序列化、传给其他库、存数据库、打印调试等等
    model_dump_json()  

