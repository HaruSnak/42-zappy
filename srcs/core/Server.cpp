#include "../../includes/Server.hpp"
#include <sys/socket.h>
#include <netinet/in.h>
#include <cstring>
#include <cerrno>
#include <cmath>
#include <iostream>
#include <algorithm>
#include <stdexcept>
#include <sstream>

namespace {
	// Elevation ritual requirements, indexed by the current level (1..7)
	struct ElevationReq { size_t players; int linemate, deraumere, sibur, mendiane, phiras, thystame; };
	const ElevationReq ELEVATION[8] = {
		{0, 0, 0, 0, 0, 0, 0},
		{1, 1, 0, 0, 0, 0, 0},
		{2, 1, 1, 1, 0, 0, 0},
		{2, 2, 0, 1, 0, 2, 0},
		{4, 1, 1, 2, 0, 1, 0},
		{4, 1, 2, 1, 3, 0, 0},
		{6, 1, 2, 3, 0, 1, 0},
		{6, 2, 2, 2, 2, 2, 1},
	};
	const size_t MAX_LEVEL = 8;
	const size_t WINNING_PLAYERS = 6;
	const int FOOD_UNITS = 126;
	const int START_LIFE_UNITS = 1260;
	const int EGG_HATCH_UNITS = 600;
	// A hatched egg nobody connects to lives like a new player, then rots (edi)
	const int EGG_LIFE_UNITS = 1260;
	const int INCANTATION_UNITS = 300;
	const int REFILL_UNITS = 20;

	// Returns the field named `name` in a Tile or an InventoryPly (same field names)
	template <typename T>
	int *resource_field(T &s, const std::string &name) {
		if (name == "nourriture") return &s.food;
		if (name == "linemate")   return &s.linemate;
		if (name == "deraumere")  return &s.deraumere;
		if (name == "sibur")      return &s.sibur;
		if (name == "mendiane")   return &s.mendiane;
		if (name == "phiras")     return &s.phiras;
		if (name == "thystame")   return &s.thystame;
		return nullptr;
	}

	// Resource number "i" of the graphic protocol (0 = nourriture ... 6 = thystame)
	int resource_index(const std::string &name) {
		static const char *names[7] = {"nourriture", "linemate", "deraumere", "sibur", "mendiane", "phiras", "thystame"};
		for (int i = 0; i < 7; ++i)
			if (name == names[i])
				return (i);
		return (-1);
	}

	std::string num(long long n) { return std::to_string(n); }
	// Player and egg numbers are written "#n" in the graphic protocol
	std::string id(int n) { return "#" + std::to_string(n); }

	// Parses a non negative number, written with or without its leading '#'
	bool parse_number(std::string s, bool sharp_allowed, int &out) {
		if (sharp_allowed && !s.empty() && s[0] == '#')
			s.erase(0, 1);
		if (s.empty() || s.size() > 9 || !std::all_of(s.begin(), s.end(), ::isdigit))
			return (false);
		out = std::stoi(s);
		return (true);
	}
}

/* ************************************************************************** */
/*                         CONSTRUCTORS & DESTRUCTOR                          */
/* ************************************************************************** */

const size_t Server::MAX_OUTPUT_BUFFER;
const size_t Server::SEND_CHUNK;

Server::~Server() {
	this->m_clients.clear();
	if (this->m_serverSocket != -1)
		close(this->m_serverSocket);
}

/* ************************************************************************** */
/*                                   TIME                                     */
/* ************************************************************************** */

// Duration of n time units: n / t seconds
Clock::duration Server::units(double n) const {
	return std::chrono::duration_cast<Clock::duration>(
		std::chrono::duration<double>(n / this->serverOPTS.time));
}

int Server::command_delay(const std::string &cmd) const {
	if (cmd == "inventaire") return 1;
	if (cmd == "fork") return 42;
	if (cmd == "connect_nbr") return 0;
	// The requirements are checked at once, the ritual itself lasts 300/t
	if (cmd == "incantation") return 0;
	return 7;
}

void Server::start_next_command(Player &p) {
	// The egg is being laid for 42/t: "pfk" now, "enw" when it is done
	if (p.front_command() == "fork")
		this->send_to_graphic("pfk " + id(p.getId()) + "\n");
	p.set_action_end_time(Clock::now() + this->units(this->command_delay(p.front_command())));
}

// poll() timeout = time until the next game event, so the server sleeps
// between events instead of spinning.
int Server::compute_timeout(void) {
	auto now = Clock::now();
	Clock::time_point next = this->m_next_refill;
	auto consider = [&next](Clock::time_point tp) { if (tp < next) next = tp; };

	if (this->m_game_over)
		consider(this->m_shutdown_deadline);
	else {
		for (auto &pair : this->m_clients) {
			Player &p = pair.second;
			if (!p.isPlayer())
				continue;
			consider(p.get_death_time());
			if (p.has_command() && !p.isFrozen())
				consider(p.get_action_end_time());
		}
		for (auto &egg : this->m_eggs)
			consider(egg.hatched ? egg.death_time : egg.hatch_time);
		for (auto &inc : this->m_incantations)
			consider(inc.end_time);
	}
	if (next <= now)
		return (0);
	long long us = std::chrono::duration_cast<std::chrono::microseconds>(next - now).count();
	return (static_cast<int>(std::min<long long>((us + 999) / 1000, 60000)));
}

// "sst T": every pending deadline keeps the same number of time units left
void Server::set_time_unit(int t) {
	auto now = Clock::now();
	double ratio = static_cast<double>(this->serverOPTS.time) / t;
	auto rescale = [now, ratio](Clock::time_point &tp) {
		if (tp > now)
			tp = now + std::chrono::duration_cast<Clock::duration>((tp - now) * ratio);
	};

	this->serverOPTS.time = t;
	for (auto &pair : this->m_clients) {
		Player &p = pair.second;
		if (!p.isPlayer())
			continue ;
		Clock::time_point death = p.get_death_time();
		rescale(death);
		p.set_death_time(death);
		Clock::time_point end = p.get_action_end_time();
		rescale(end);
		p.set_action_end_time(end);
	}
	for (auto &egg : this->m_eggs) {
		rescale(egg.hatch_time);
		rescale(egg.death_time);
	}
	for (auto &inc : this->m_incantations)
		rescale(inc.end_time);
	rescale(this->m_next_refill);
}

/* ************************************************************************** */
/*                                  NETWORK                                   */
/* ************************************************************************** */

void Server::init_socket(void) {
	this->m_serverSocket = socket(AF_INET, SOCK_STREAM, 0);
	if (this->m_serverSocket == -1)
		throw std::runtime_error(std::string("socket: ") + strerror(errno));
	int opt = 1;
	if (setsockopt(this->m_serverSocket, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) == -1)
		std::cerr << "Warning: setsockopt failed" << std::endl;

	struct sockaddr_in serverAddress;
	memset(&serverAddress, 0, sizeof(serverAddress));
	serverAddress.sin_family = AF_INET;
	serverAddress.sin_port = htons(this->serverOPTS.port);
	serverAddress.sin_addr.s_addr = INADDR_ANY;

	if (bind(this->m_serverSocket, (struct sockaddr*)&serverAddress, sizeof(serverAddress)) == -1)
		throw std::runtime_error("bind: port " + num(this->serverOPTS.port) + ": " + strerror(errno));
	if (listen(this->m_serverSocket, 64) == -1)
		throw std::runtime_error(std::string("listen: ") + strerror(errno));
}

void Server::accept_client(void) {
	int fd = accept(this->m_serverSocket, NULL, NULL);
	if (fd == -1) {
		std::cerr << "accept: " << strerror(errno) << std::endl;
		return ;
	}
	this->m_clients.emplace(std::piecewise_construct,
		std::forward_as_tuple(fd), std::forward_as_tuple(fd));
	this->send_to(fd, "BIENVENUE\n");
	std::cout << "New client connected | FD: " << fd << std::endl;
}

void Server::read_client(int fd) {
	char buffer[4096];
	ssize_t n = recv(fd, buffer, sizeof(buffer), 0);
	if (n <= 0) {
		this->m_to_disconnect.insert(fd);
		return ;
	}
	this->m_clients.at(fd).append_input(buffer, n);
	this->process_input(fd);
}

// Writes at most SEND_CHUNK bytes; only called when poll() reported POLLOUT,
// so the (blocking) socket has room and send() returns without waiting.
void Server::flush_client(int fd) {
	Player &p = this->m_clients.at(fd);
	std::string &out = p.get_output();
	if (!out.empty()) {
		ssize_t n = send(fd, out.data(), std::min(out.size(), SEND_CHUNK), MSG_NOSIGNAL);
		if (n < 0) {
			if (errno != EINTR)
				this->m_to_disconnect.insert(fd);
			return ;
		}
		out.erase(0, n);
	}
	if (out.empty() && p.isDead())
		this->m_to_disconnect.insert(fd);
}

// Never writes on the socket directly: the message is buffered and sent
// when poll() says the socket is writable.
void Server::send_to(int fd, const std::string &msg) {
	auto it = this->m_clients.find(fd);
	if (it == this->m_clients.end() || this->m_to_disconnect.count(fd))
		return ;
	it->second.queue_output(msg);
	if (it->second.get_output().size() > MAX_OUTPUT_BUFFER) {
		std::cerr << "Client FD " << fd << " does not read its data, disconnecting" << std::endl;
		this->m_to_disconnect.insert(fd);
	}
}

void Server::send_to_graphic(const std::string &msg) {
	for (auto &pair : this->m_clients)
		if (pair.second.isGraphic())
			this->send_to(pair.first, msg);
}

bool Server::all_output_flushed(void) const {
	for (auto &pair : this->m_clients)
		if (pair.second.has_output())
			return (false);
	return (true);
}

void Server::disconnect_client(int fd) {
	auto it = this->m_clients.find(fd);
	if (it == this->m_clients.end())
		return ;
	bool was_player = it->second.isPlayer();
	int player_id = it->second.getId();
	this->m_clients.erase(it);
	// The team slot is not given back: a team is made of n players and only n
	if (was_player)
		this->send_to_graphic("pdi " + id(player_id) + "\n");
	std::cout << "Client disconnected | FD: " << fd << std::endl;
}

void Server::process_input(int fd) {
	Player &p = this->m_clients.at(fd);
	std::string line;

	while (p.pop_line(line)) {
		if (!p.isAuthenticated()) {
			this->handshake(fd, line);
			continue ;
		}
		if (p.isGraphic()) {
			this->handle_graphic_command(fd, line);
			continue ;
		}
		// Dead players do not play
		if (!p.isPlayer() || this->m_game_over)
			continue ;
		bool was_empty = !p.has_command();
		// Beyond 10 pending requests the command is ignored
		if (p.push_command(line) && was_empty && !p.isFrozen())
			this->start_next_command(p);
	}
}

void Server::handshake(int fd, const std::string &line) {
	Player &p = this->m_clients.at(fd);

	if (line == "GRAPHIC") {
		p.setGraphic(true);
		this->graphic_handshake(fd);
		return ;
	}
	auto team_it = std::find(this->serverOPTS.teams.begin(), this->serverOPTS.teams.end(), line);
	if (team_it == this->serverOPTS.teams.end() || this->m_team_slots[line] <= 0) {
		this->send_to(fd, "ko\n");
		return ;
	}
	this->m_team_slots[line]--;
	p.setTeam(line);
	p.setId(this->m_player_id_counter++);

	// A hatched egg of the team gives its position to the new player
	auto egg = std::find_if(this->m_eggs.begin(), this->m_eggs.end(),
		[&line](const Egg &e) { return e.team == line && e.hatched; });
	if (egg != this->m_eggs.end()) {
		p.setX(egg->x);
		p.setY(egg->y);
		this->send_to_graphic("ebo " + id(egg->id) + "\n");
		this->m_eggs.erase(egg);
	} else {
		p.setX(rand() % this->serverOPTS.x);
		p.setY(rand() % this->serverOPTS.y);
	}
	p.setOrientation(static_cast<Player::Direction>(rand() % 4));
	p.set_death_time(Clock::now() + this->units(START_LIFE_UNITS));

	this->send_to(fd, num(this->m_team_slots[line]) + "\n");
	this->send_to(fd, num(this->serverOPTS.x) + " " + num(this->serverOPTS.y) + "\n");
	this->send_to_graphic(this->get_pnw_string(p));
	std::cout << "FD " << fd << " joined team " << line << std::endl;
}

// Monitor connection: msz, sgt, bct (every tile), tna (every team), pnw
// (every player), enw (every egg). The state the monitor cannot rebuild
// otherwise (hatched eggs, rituals in progress) follows with eht and pic.
void Server::graphic_handshake(int fd) {
	std::string msg = "msz " + num(this->serverOPTS.x) + " " + num(this->serverOPTS.y) + "\n";
	msg += "sgt " + num(this->serverOPTS.time) + "\n";
	msg += this->get_mct_string();
	for (auto &t : this->serverOPTS.teams)
		msg += "tna " + t + "\n";
	for (auto &pair : this->m_clients)
		if (pair.second.isPlayer())
			msg += this->get_pnw_string(pair.second);
	for (auto &egg : this->m_eggs)
		msg += "enw " + id(egg.id) + " " + id(egg.parent_id) + " " + num(egg.x) + " " + num(egg.y) + "\n";
	for (auto &egg : this->m_eggs)
		if (egg.hatched)
			msg += "eht " + id(egg.id) + "\n";
	for (auto &inc : this->m_incantations) {
		msg += "pic " + num(inc.x) + " " + num(inc.y) + " " + num(inc.level);
		for (int pfd : inc.fds) {
			auto c = this->m_clients.find(pfd);
			if (c != this->m_clients.end() && c->second.isPlayer())
				msg += " " + id(c->second.getId());
		}
		msg += "\n";
	}
	this->send_to(fd, msg);
	std::cout << "Graphic client connected | FD: " << fd << std::endl;
}

/* ************************************************************************** */
/*                            GRAPHIC PROTOCOL                                */
/* ************************************************************************** */

std::string Server::get_bct_string(int x, int y) {
	World::Tile &t = this->m_world.getTile(x, y);
	return "bct " + num(x) + " " + num(y) + " " + num(t.food) + " " + num(t.linemate) + " "
		+ num(t.deraumere) + " " + num(t.sibur) + " " + num(t.mendiane) + " "
		+ num(t.phiras) + " " + num(t.thystame) + "\n";
}

std::string Server::get_mct_string(void) {
	std::string mct;
	for (int y = 0; y < this->serverOPTS.y; ++y)
		for (int x = 0; x < this->serverOPTS.x; ++x)
			mct += this->get_bct_string(x, y);
	return mct;
}

// Orientation for the graphic client: 1=N, 2=E, 3=S, 4=W
std::string Server::get_ppo_string(Player &p) {
	return "ppo " + id(p.getId()) + " " + num(p.getX()) + " " + num(p.getY()) + " "
		+ num(p.getOrientation() + 1) + "\n";
}

std::string Server::get_pnw_string(Player &p) {
	return "pnw " + id(p.getId()) + " " + num(p.getX()) + " " + num(p.getY()) + " "
		+ num(p.getOrientation() + 1) + " " + num(p.getLevel()) + " " + p.getTeam() + "\n";
}

std::string Server::get_plv_string(Player &p) {
	return "plv " + id(p.getId()) + " " + num(p.getLevel()) + "\n";
}

std::string Server::get_pin_string(Player &p) {
	Player::InventoryPly &inv = p.get_inventoryPly();
	return "pin " + id(p.getId()) + " " + num(p.getX()) + " " + num(p.getY()) + " "
		+ num(inv.food) + " " + num(inv.linemate) + " " + num(inv.deraumere) + " "
		+ num(inv.sibur) + " " + num(inv.mendiane) + " " + num(inv.phiras) + " "
		+ num(inv.thystame) + "\n";
}

Player *Server::find_player_by_id(int player_id) {
	for (auto &pair : this->m_clients)
		if (pair.second.isPlayer() && pair.second.getId() == player_id)
			return (&pair.second);
	return (nullptr);
}

// Requests of the monitor: msz, bct X Y, mct, tna, ppo #n, plv #n, pin #n,
// sgt, sst T. "suc" for an unknown command, "sbp" for bad parameters.
void Server::handle_graphic_command(int fd, const std::string &line) {
	std::istringstream iss(line);
	std::vector<std::string> a;
	for (std::string word; iss >> word; )
		a.push_back(word);
	if (a.empty()) {
		this->send_to(fd, "suc\n");
		return ;
	}
	const std::string &cmd = a[0];
	size_t argc = a.size() - 1;
	int n1, n2;

	if (cmd == "msz" || cmd == "mct" || cmd == "tna" || cmd == "sgt") {
		if (argc != 0)
			this->send_to(fd, "sbp\n");
		else if (cmd == "msz")
			this->send_to(fd, "msz " + num(this->serverOPTS.x) + " " + num(this->serverOPTS.y) + "\n");
		else if (cmd == "mct")
			this->send_to(fd, this->get_mct_string());
		else if (cmd == "tna") {
			std::string msg;
			for (auto &t : this->serverOPTS.teams)
				msg += "tna " + t + "\n";
			this->send_to(fd, msg);
		}
		else
			this->send_to(fd, "sgt " + num(this->serverOPTS.time) + "\n");
	}
	else if (cmd == "bct") {
		if (argc != 2 || !parse_number(a[1], false, n1) || !parse_number(a[2], false, n2)
			|| n1 >= this->serverOPTS.x || n2 >= this->serverOPTS.y)
			this->send_to(fd, "sbp\n");
		else
			this->send_to(fd, this->get_bct_string(n1, n2));
	}
	else if (cmd == "ppo" || cmd == "plv" || cmd == "pin") {
		Player *p = nullptr;
		if (argc == 1 && parse_number(a[1], true, n1))
			p = this->find_player_by_id(n1);
		if (p == nullptr)
			this->send_to(fd, "sbp\n");
		else if (cmd == "ppo")
			this->send_to(fd, this->get_ppo_string(*p));
		else if (cmd == "plv")
			this->send_to(fd, this->get_plv_string(*p));
		else
			this->send_to(fd, this->get_pin_string(*p));
	}
	else if (cmd == "sst") {
		if (argc != 1 || !parse_number(a[1], false, n1) || n1 < 1 || n1 > 1000000)
			this->send_to(fd, "sbp\n");
		else {
			this->set_time_unit(n1);
			// Every monitor learns the new time unit
			this->send_to_graphic("sgt " + num(n1) + "\n");
		}
	}
	else
		this->send_to(fd, "suc\n");
}

/* ************************************************************************** */
/*                                GAME LOGIC                                  */
/* ************************************************************************** */

void Server::kill_player(int fd) {
	Player &p = this->m_clients.at(fd);
	this->send_to(fd, "mort\n");
	this->send_to_graphic("pdi " + id(p.getId()) + "\n");
	// The connection is closed once "mort" has been sent (see flush_client)
	p.setDead();
	std::cout << "Player FD " << fd << " died of hunger." << std::endl;
}

void Server::update_game(void) {
	auto now = Clock::now();
	double seconds_per_food = static_cast<double>(FOOD_UNITS) / this->serverOPTS.time;

	// Hunger: the life time is absolute, the food count is derived from it
	for (auto &pair : this->m_clients) {
		Player &p = pair.second;
		if (!p.isPlayer())
			continue ;
		if (p.get_death_time() <= now) {
			this->kill_player(pair.first);
			continue ;
		}
		double remaining = std::chrono::duration<double>(p.get_death_time() - now).count();
		int food = static_cast<int>(std::ceil(remaining / seconds_per_food));
		if (food != p.get_inventoryPly().food) {
			p.get_inventoryPly().food = food;
			this->send_to_graphic(this->get_pin_string(p));
		}
	}

	// Eggs: a hatched egg authorizes one more connection for its team,
	// until it rots if nobody connects to it
	for (auto it = this->m_eggs.begin(); it != this->m_eggs.end(); ) {
		if (!it->hatched && it->hatch_time <= now) {
			it->hatched = true;
			it->death_time = it->hatch_time + this->units(EGG_LIFE_UNITS);
			this->m_team_slots[it->team]++;
			this->send_to_graphic("eht " + id(it->id) + "\n");
		}
		if (it->hatched && it->death_time <= now) {
			this->m_team_slots[it->team]--;
			this->send_to_graphic("edi " + id(it->id) + "\n");
			it = this->m_eggs.erase(it);
			continue ;
		}
		++it;
	}

	// Commands whose delay is over (at most one per player per turn)
	for (auto &pair : this->m_clients) {
		Player &p = pair.second;
		if (!p.isPlayer() || p.isFrozen() || !p.has_command() || p.get_action_end_time() > now)
			continue ;
		std::string cmd = p.front_command();
		p.pop_command();
		this->execute_player_command(pair.first, cmd);
		if (p.isPlayer() && !p.isFrozen() && p.has_command())
			this->start_next_command(p);
	}

	this->check_finished_incantations();

	if (now >= this->m_next_refill) {
		for (auto &pos : this->m_world.refillResources())
			this->send_to_graphic(this->get_bct_string(pos.first, pos.second));
		this->m_next_refill = now + this->units(REFILL_UNITS);
	}
}

std::string Server::build_vision_string(int self_fd, Player &player) {
	std::string res = "{";
	int L = static_cast<int>(player.getLevel());
	int px = player.getX();
	int py = player.getY();
	Player::Direction dir = player.getOrientation();

	for (int d = 0; d <= L; ++d) {
		for (int offset = -d; offset <= d; ++offset) {
			int tx = px, ty = py;
			if (dir == Player::NORTH)      { ty -= d; tx += offset; }
			else if (dir == Player::EAST)  { tx += d; ty += offset; }
			else if (dir == Player::SOUTH) { ty += d; tx -= offset; }
			else                           { tx -= d; ty -= offset; }
			tx = this->m_world.clampX(tx);
			ty = this->m_world.clampY(ty);

			World::Tile &tile = this->m_world.getTile(tx, ty);
			std::string content;
			// A player does not see himself
			for (auto &pair : this->m_clients)
				if (pair.second.isPlayer() && pair.first != self_fd
					&& pair.second.getX() == tx && pair.second.getY() == ty)
					content += "player ";
			for (int i = 0; i < tile.food; i++)      content += "nourriture ";
			for (int i = 0; i < tile.linemate; i++)  content += "linemate ";
			for (int i = 0; i < tile.deraumere; i++) content += "deraumere ";
			for (int i = 0; i < tile.sibur; i++)     content += "sibur ";
			for (int i = 0; i < tile.mendiane; i++)  content += "mendiane ";
			for (int i = 0; i < tile.phiras; i++)    content += "phiras ";
			for (int i = 0; i < tile.thystame; i++)  content += "thystame ";
			if (!content.empty())
				content.pop_back();

			res += content;
			if (d != L || offset != d)
				res += ", ";
		}
	}
	return res + "}\n";
}

void Server::execute_player_command(int fd, const std::string &cmd) {
	Player &player = this->m_clients.at(fd);

	if (cmd == "avance") {
		if (player.getOrientation() == Player::NORTH)      player.setY(this->m_world.clampY(player.getY() - 1));
		else if (player.getOrientation() == Player::EAST)  player.setX(this->m_world.clampX(player.getX() + 1));
		else if (player.getOrientation() == Player::SOUTH) player.setY(this->m_world.clampY(player.getY() + 1));
		else                                               player.setX(this->m_world.clampX(player.getX() - 1));
		this->send_to(fd, "ok\n");
		this->send_to_graphic(this->get_ppo_string(player));
	}
	else if (cmd == "droite" || cmd == "gauche") {
		int step = (cmd == "droite") ? 1 : 3;
		player.setOrientation(static_cast<Player::Direction>((player.getOrientation() + step) % 4));
		this->send_to(fd, "ok\n");
		this->send_to_graphic(this->get_ppo_string(player));
	}
	else if (cmd == "voir") {
		this->send_to(fd, this->build_vision_string(fd, player));
	}
	else if (cmd == "inventaire") {
		Player::InventoryPly &inv = player.get_inventoryPly();
		this->send_to(fd, "{nourriture " + num(inv.food) + ", linemate " + num(inv.linemate)
			+ ", deraumere " + num(inv.deraumere) + ", sibur " + num(inv.sibur)
			+ ", mendiane " + num(inv.mendiane) + ", phiras " + num(inv.phiras)
			+ ", thystame " + num(inv.thystame) + "}\n");
	}
	else if (cmd.compare(0, 6, "prend ") == 0 || cmd.compare(0, 5, "pose ") == 0) {
		bool take = (cmd[1] == 'r');
		std::string item = cmd.substr(take ? 6 : 5);
		World::Tile &tile = this->m_world.getTile(player.getX(), player.getY());
		int *on_tile = resource_field(tile, item);
		int *in_inv = resource_field(player.get_inventoryPly(), item);
		int *from = take ? on_tile : in_inv;
		int *to = take ? in_inv : on_tile;

		if (from == nullptr || *from <= 0) {
			this->send_to(fd, "ko\n");
			return ;
		}
		(*from)--;
		(*to)++;
		// Food is life time: one unit = 126 time units
		if (item == "nourriture")
			player.add_life(take ? this->units(FOOD_UNITS) : -this->units(FOOD_UNITS));
		this->send_to(fd, "ok\n");
		this->send_to_graphic((take ? "pgt " : "pdr ") + id(player.getId()) + " " + num(resource_index(item)) + "\n");
		this->send_to_graphic(this->get_pin_string(player));
		this->send_to_graphic(this->get_bct_string(player.getX(), player.getY()));
	}
	else if (cmd.compare(0, 10, "broadcast ") == 0) {
		std::string text = cmd.substr(10);
		for (auto &pair : this->m_clients) {
			Player &other = pair.second;
			if (!other.isPlayer() || pair.first == fd)
				continue ;
			int K = this->get_broadcast_dir(player.getX(), player.getY(),
				other.getX(), other.getY(), other.getOrientation());
			this->send_to(pair.first, "message " + num(K) + "," + text + "\n");
		}
		this->send_to(fd, "ok\n");
		this->send_to_graphic("pbc " + id(player.getId()) + " " + text + "\n");
	}
	else if (cmd == "expulse") {
		int px = player.getX();
		int py = player.getY();
		std::vector<int> pushed;

		for (auto &pair : this->m_clients) {
			Player &other = pair.second;
			if (!other.isPlayer() || pair.first == fd || other.getX() != px || other.getY() != py)
				continue ;
			if (player.getOrientation() == Player::NORTH)      other.setY(this->m_world.clampY(py - 1));
			else if (player.getOrientation() == Player::EAST)  other.setX(this->m_world.clampX(px + 1));
			else if (player.getOrientation() == Player::SOUTH) other.setY(this->m_world.clampY(py + 1));
			else                                               other.setX(this->m_world.clampX(px - 1));
			// K = direction of the square the pushed player comes from
			int K = this->get_broadcast_dir(px, py, other.getX(), other.getY(), other.getOrientation());
			this->send_to(pair.first, "deplacement " + num(K) + "\n");
			pushed.push_back(pair.first);
		}
		if (pushed.empty()) {
			this->send_to(fd, "ko\n");
			return ;
		}
		this->send_to(fd, "ok\n");
		// "pex" first, then the new position of every pushed player
		this->send_to_graphic("pex " + id(player.getId()) + "\n");
		for (int pfd : pushed)
			this->send_to_graphic(this->get_ppo_string(this->m_clients.at(pfd)));
	}
	else if (cmd == "incantation") {
		int x = player.getX();
		int y = player.getY();
		size_t lvl = player.getLevel();

		if (lvl >= MAX_LEVEL || this->count_players_for_incantation(x, y, lvl) < ELEVATION[lvl].players
			|| !this->check_incantation_stones(x, y, lvl)) {
			this->send_to(fd, "ko\n");
			return ;
		}
		Incantation inc;
		inc.x = x;
		inc.y = y;
		inc.level = lvl;
		inc.end_time = Clock::now() + this->units(INCANTATION_UNITS);
		std::string pic = "pic " + num(x) + " " + num(y) + " " + num(lvl);
		for (auto &pair : this->m_clients) {
			Player &p = pair.second;
			if (p.isPlayer() && !p.isFrozen() && p.getX() == x && p.getY() == y && p.getLevel() == lvl) {
				inc.fds.push_back(pair.first);
				p.setFrozen(true);
				this->send_to(pair.first, "elevation en cours\n");
				pic += " " + id(p.getId());
			}
		}
		this->m_incantations.push_back(inc);
		this->send_to_graphic(pic + "\n");
	}
	else if (cmd == "fork") {
		Egg egg;
		egg.id = this->m_egg_id_counter++;
		egg.parent_id = player.getId();
		egg.team = player.getTeam();
		egg.x = player.getX();
		egg.y = player.getY();
		egg.hatch_time = Clock::now() + this->units(EGG_HATCH_UNITS);
		this->m_eggs.push_back(egg);
		this->send_to(fd, "ok\n");
		this->send_to_graphic("enw " + id(egg.id) + " " + id(egg.parent_id) + " " + num(egg.x) + " " + num(egg.y) + "\n");
	}
	else if (cmd == "connect_nbr") {
		this->send_to(fd, num(this->m_team_slots[player.getTeam()]) + "\n");
	}
	else {
		this->send_to(fd, "ko\n");
	}
}

// Number of the square (1..8, 0 = same square) the sound comes from, seen
// from the receiver: 1 is in front, then counter-clockwise.
int Server::get_broadcast_dir(int src_x, int src_y, int dst_x, int dst_y, Player::Direction dir) {
	if (src_x == dst_x && src_y == dst_y)
		return (0);

	int w = this->serverOPTS.x;
	int h = this->serverOPTS.y;
	int dx = src_x - dst_x;
	int dy = src_y - dst_y;

	// The world is round: take the shortest path
	if (dx > w / 2) dx -= w; else if (dx < -w / 2) dx += w;
	if (dy > h / 2) dy -= h; else if (dy < -h / 2) dy += h;

	double angle = atan2(dy, dx);
	double receiver_angle = 0;
	if (dir == Player::NORTH)      receiver_angle = -M_PI / 2.0;
	else if (dir == Player::SOUTH) receiver_angle = M_PI / 2.0;
	else if (dir == Player::WEST)  receiver_angle = M_PI;

	double rel = angle - receiver_angle;
	while (rel <= -M_PI) rel += 2.0 * M_PI;
	while (rel > M_PI) rel -= 2.0 * M_PI;

	// y grows downwards: a negative relative angle is on the receiver's left
	double pi_8 = M_PI / 8.0;
	if (rel >= -pi_8 && rel < pi_8) return (1);
	if (rel >= -3 * pi_8 && rel < -pi_8) return (2);
	if (rel >= -5 * pi_8 && rel < -3 * pi_8) return (3);
	if (rel >= -7 * pi_8 && rel < -5 * pi_8) return (4);
	if (rel >= 5 * pi_8 && rel < 7 * pi_8) return (6);
	if (rel >= 3 * pi_8 && rel < 5 * pi_8) return (7);
	if (rel >= pi_8 && rel < 3 * pi_8) return (8);
	return (5);
}

size_t Server::count_players_for_incantation(int x, int y, size_t level) {
	size_t n = 0;
	for (auto &pair : this->m_clients) {
		Player &p = pair.second;
		if (p.isPlayer() && !p.isFrozen() && p.getX() == x && p.getY() == y && p.getLevel() == level)
			n++;
	}
	return (n);
}

bool Server::check_incantation_stones(int x, int y, size_t level) {
	if (level < 1 || level >= MAX_LEVEL)
		return (false);
	const ElevationReq &r = ELEVATION[level];
	World::Tile &t = this->m_world.getTile(x, y);
	return (t.linemate >= r.linemate && t.deraumere >= r.deraumere && t.sibur >= r.sibur
		&& t.mendiane >= r.mendiane && t.phiras >= r.phiras && t.thystame >= r.thystame);
}

void Server::check_finished_incantations(void) {
	auto now = Clock::now();

	for (auto it = this->m_incantations.begin(); it != this->m_incantations.end(); ) {
		if (it->end_time > now) {
			++it;
			continue ;
		}
		Incantation inc = *it;
		it = this->m_incantations.erase(it);

		// Participants still alive, on the square and at the right level
		std::vector<int> present;
		for (int fd : inc.fds) {
			auto c = this->m_clients.find(fd);
			if (c == this->m_clients.end() || !c->second.isPlayer())
				continue ;
			c->second.setFrozen(false);
			if (c->second.getX() == inc.x && c->second.getY() == inc.y && c->second.getLevel() == inc.level)
				present.push_back(fd);
		}
		bool success = present.size() >= ELEVATION[inc.level].players
			&& this->check_incantation_stones(inc.x, inc.y, inc.level);

		if (success) {
			const ElevationReq &r = ELEVATION[inc.level];
			World::Tile &t = this->m_world.getTile(inc.x, inc.y);
			t.linemate -= r.linemate;
			t.deraumere -= r.deraumere;
			t.sibur -= r.sibur;
			t.mendiane -= r.mendiane;
			t.phiras -= r.phiras;
			t.thystame -= r.thystame;
		}
		// Graphic order: pie, then plv of each participant, then bct of the tile
		this->send_to_graphic("pie " + num(inc.x) + " " + num(inc.y) + " " + (success ? "1" : "0") + "\n");
		for (int fd : inc.fds) {
			auto c = this->m_clients.find(fd);
			if (c == this->m_clients.end() || !c->second.isPlayer())
				continue ;
			Player &p = c->second;
			if (success && std::find(present.begin(), present.end(), fd) != present.end())
				p.setLevel(inc.level + 1);
			this->send_to_graphic(this->get_plv_string(p));
			this->send_to(fd, "niveau actuel : " + num(p.getLevel()) + "\n");
			// The commands received during the ritual now start their own delay
			if (p.has_command())
				this->start_next_command(p);
		}
		this->send_to_graphic(this->get_bct_string(inc.x, inc.y));
		if (success)
			this->check_victory();
	}
}

// The winning team is the first one with 6 players at the maximum level
void Server::check_victory(void) {
	if (this->m_game_over)
		return ;
	std::map<std::string, size_t> max_level_count;
	for (auto &pair : this->m_clients) {
		Player &p = pair.second;
		if (!p.isPlayer() || p.getLevel() < MAX_LEVEL)
			continue ;
		if (++max_level_count[p.getTeam()] >= WINNING_PLAYERS) {
			std::cout << "VICTOIRE DE L'EQUIPE : " << p.getTeam() << std::endl;
			this->send_to_graphic("seg " + p.getTeam() + "\n");
			this->m_game_over = true;
			// Let the graphic clients receive the end of the game before closing
			this->m_shutdown_deadline = Clock::now() + std::chrono::seconds(3);
			return ;
		}
	}
}

/* ************************************************************************** */
/*                                 MAIN LOOP                                  */
/* ************************************************************************** */

void Server::run(void) {
	this->init_socket();
	this->m_world = World(this->serverOPTS.x, this->serverOPTS.y);
	this->m_world.spawnResources();
	this->m_next_refill = Clock::now() + this->units(REFILL_UNITS);
	for (const auto &t : this->serverOPTS.teams)
		this->m_team_slots[t] = this->serverOPTS.nbClients;
	std::cout << "Server listening on port " << this->serverOPTS.port << std::endl;

	while (g_keep_running) {
		// POLLOUT is only requested for clients that have pending output
		std::vector<struct pollfd> fds;
		fds.push_back({this->m_serverSocket, POLLIN, 0});
		for (auto &pair : this->m_clients) {
			short events = POLLIN;
			if (pair.second.has_output())
				events |= POLLOUT;
			fds.push_back({pair.first, events, 0});
		}

		int ret = poll(fds.data(), fds.size(), this->compute_timeout());
		if (ret < 0) {
			if (errno == EINTR)
				continue ;
			std::cerr << "poll: " << strerror(errno) << std::endl;
			break ;
		}
		for (auto &pfd : fds) {
			if (pfd.revents == 0)
				continue ;
			if (pfd.fd == this->m_serverSocket) {
				if (pfd.revents & POLLIN)
					this->accept_client();
				continue ;
			}
			if (this->m_clients.find(pfd.fd) == this->m_clients.end() || this->m_to_disconnect.count(pfd.fd))
				continue ;
			if (pfd.revents & (POLLERR | POLLNVAL)) {
				this->m_to_disconnect.insert(pfd.fd);
				continue ;
			}
			if (pfd.revents & (POLLIN | POLLHUP))
				this->read_client(pfd.fd);
			if ((pfd.revents & POLLOUT) && !this->m_to_disconnect.count(pfd.fd))
				this->flush_client(pfd.fd);
		}

		if (!this->m_game_over)
			this->update_game();

		while (!this->m_to_disconnect.empty()) {
			int fd = *this->m_to_disconnect.begin();
			this->m_to_disconnect.erase(this->m_to_disconnect.begin());
			this->disconnect_client(fd);
		}

		if (this->m_game_over && (this->all_output_flushed() || Clock::now() >= this->m_shutdown_deadline))
			break ;
	}
	std::cout << "Server shutting down" << std::endl;
}
