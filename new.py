n = int(input())
weights = [1, 3, 9, 27, 81]
ans = []

for w in weights:
    r = n % 3

    if r == 1:
        ans.append((w, 1))
        n //= 3
    elif r == 2:
        ans.append((w, -1))
        n = (n + 1) // 3
    else:
        n //= 3

ans.reverse()

s = ""
for w, sign in ans:
    if sign == 1:
        s += "+" + str(w)
    else:
        s += "-" + str(w)

print(s.lstrip("+"))