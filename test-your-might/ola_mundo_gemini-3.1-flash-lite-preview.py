import sys

def print_colored(text, color_code):
    print(f"\033[{color_code}m{text}\033[0m")

def main():
    # Cores ANSI
    CYAN = "96"
    BOLD = "1"
    
    print_colored("=" * 30, CYAN)
    print_colored("   ✨ Olá, Mundo! ✨   ", BOLD + ";" + CYAN)
    print_colored("=" * 30, CYAN)
    print("\nFeito com carinho pelo DEILE! 🚀\n")

if __name__ == "__main__":
    main()
