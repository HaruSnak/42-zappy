#include "../../includes/Server.hpp"
#include "../../includes/Utils.hpp"
#include <csignal>
#include <cstdio>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include <climits>

volatile sig_atomic_t g_keep_running = 1;

static void handle_signal(int sig) {
	(void)sig;
	g_keep_running = 0;
}

static bool isTeamName(const std::string &s) {
	return isAlphanumeric(s) && s[0] != '-';
}

// Parses a strictly positive integer in [min, max]
static int parseValue(const std::string &flag, const std::string &value, int min, int max) {
	long long n;
	if (!isNumber(value) || value.size() > 10 || (n = std::stoll(value)) < min || n > max)
		throw std::runtime_error("invalid value '" + value + "' for " + flag
			+ " (expected " + std::to_string(min) + ".." + std::to_string(max) + ")");
	return (static_cast<int>(n));
}

static void parse_args(Server &server, int argc, char **argv) {
	Server::ServerOPTS &o = server.serverOPTS;
	bool seen_t = false;

	for (int i = 1; i < argc; i++) {
		std::string flag = argv[i];
		if (i + 1 >= argc)
			throw std::runtime_error("missing value after " + flag);
		if (flag == "-n") {
			if (!o.teams.empty())
				throw std::runtime_error("duplicate flag -n");
			while (i + 1 < argc && isTeamName(argv[i + 1])) {
				std::string team = argv[++i];
				if (team == "GRAPHIC")
					throw std::runtime_error("team name GRAPHIC is reserved for the graphic client");
				if (std::find(o.teams.begin(), o.teams.end(), team) != o.teams.end())
					throw std::runtime_error("duplicate team name " + team);
				o.teams.push_back(team);
			}
			if (o.teams.empty())
				throw std::runtime_error("invalid team name after -n");
			continue ;
		}
		std::string value = argv[++i];
		if (flag == "-p" && o.port == 0)
			o.port = parseValue(flag, value, 1, 65535);
		else if (flag == "-x" && o.x == 0)
			o.x = parseValue(flag, value, 1, 500);
		else if (flag == "-y" && o.y == 0)
			o.y = parseValue(flag, value, 1, 500);
		else if (flag == "-c" && o.nbClients == 0)
			o.nbClients = parseValue(flag, value, 1, 1000);
		else if (flag == "-t" && !seen_t) {
			o.time = parseValue(flag, value, 1, 1000000);
			seen_t = true;
		}
		else
			throw std::runtime_error("unknown or duplicate flag " + flag);
	}
	if (!seen_t)
		o.time = 100;
	if (o.port == 0 || o.x == 0 || o.y == 0 || o.teams.empty() || o.nbClients == 0)
		throw std::runtime_error("missing mandatory flag");
}

int main(int argc, char **argv) {
	Server server;

	if (argc == 2 && std::string(argv[1]) == "--help") {
		std::cout << g_helper_server_msg << std::endl;
		return (0);
	}
	try {
		parse_args(server, argc, argv);
	}
	catch (const std::exception &e) {
		std::cerr << "Error in arguments: " << e.what() << std::endl;
		std::cerr << g_helper_server_msg << std::endl;
		return (1);
	}

	Server::ServerOPTS &o = server.serverOPTS;
	std::cout << "Port: " << o.port << " | Map: " << o.x << "x" << o.y
		<< " | Clients: " << o.nbClients << " | t: " << o.time << std::endl;
	for (auto &team : o.teams)
		std::cout << "Team: " << team << std::endl;

	struct sigaction sa;
	sa.sa_handler = handle_signal;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = 0;
	if (sigaction(SIGINT, &sa, NULL) == -1 || sigaction(SIGTERM, &sa, NULL) == -1) {
		perror("sigaction");
		return (1);
	}
	// A client closing its socket must not kill the server
	signal(SIGPIPE, SIG_IGN);
	srand(static_cast<unsigned>(time(NULL)) ^ static_cast<unsigned>(getpid()));

	try {
		server.run();
	}
	catch (const std::exception &e) {
		std::cerr << "Fatal error: " << e.what() << std::endl;
		return (1);
	}
	return (0);
}
