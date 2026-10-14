#pragma once
#include <string>
#include <algorithm>
#include <cctype>

static const char *g_helper_server_msg = "Usage: ./server -p <port> -x <width> -y <height> -n <team> [<team>] [<team>] ... -c <nb> -t <t>\n"
	"\t-p port number\n"
	"\t-x world width\n"
	"\t-y world height\n"
	"\t-n team_name_1 team_name_2 ...\n"
	"\t-c number of clients authorized at the beginning of the game\n"
	"\t-t time unit divider (the greater t is, the faster the game will go)";

inline bool isNumber(const std::string& s) {
	return !s.empty() && std::all_of(s.begin(), s.end(), ::isdigit);
}

inline bool isAlphanumeric(const std::string& s) {
	return !s.empty() && std::all_of(s.begin(), s.end(), [](unsigned char c) {
		return (std::isalnum(c) || c == '_' || c == '-');
	});
}
