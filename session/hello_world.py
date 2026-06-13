print("Hello World")

def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    if b == 0:
        return "Error: Division by zero"
    return a / b

def calculator():
    print("\n--- Simple Calculator ---")
    print("Operations: add, subtract, multiply, divide")
    
    try:
        num1 = float(input("Enter first number: "))
        operation = input("Enter operation (add/subtract/multiply/divide): ").lower()
        num2 = float(input("Enter second number: "))
        
        if operation == "add":
            result = add(num1, num2)
        elif operation == "subtract":
            result = subtract(num1, num2)
        elif operation == "multiply":
            result = multiply(num1, num2)
        elif operation == "divide":
            result = divide(num1, num2)
        else:
            result = "Error: Unknown operation"
        
        print(f"Result: {result}")
    except ValueError:
        print("Error: Invalid input. Please enter valid numbers.")

if __name__ == "__main__":
    calculator()
