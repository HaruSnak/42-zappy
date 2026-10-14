#include "../../includes/Player.hpp"

/* ************************************************************************** */
/*                         CONSTRUCTORS & DESTRUCTOR                          */
/* ************************************************************************** */

Player::Player(int fd) : m_fd(fd) {
}

Player::~Player() {
	if (this->m_fd >= 0)
		close(this->m_fd);
}

/* ************************************************************************** */
/*                                  FUNCTIONS                                 */
/* ************************************************************************** */

void Player::append_input(const char *data, size_t len) {
	this->m_in_buffer.append(data, len);
	// A line without '\n' this long is garbage: drop it instead of growing forever
	if (this->m_in_buffer.size() > MAX_LINE_LENGTH
		&& this->m_in_buffer.find('\n') == std::string::npos)
		this->m_in_buffer.clear();
}

bool Player::pop_line(std::string &line) {
	size_t pos = this->m_in_buffer.find('\n');
	if (pos == std::string::npos)
		return (false);
	line = this->m_in_buffer.substr(0, pos);
	this->m_in_buffer.erase(0, pos + 1);
	if (!line.empty() && line.back() == '\r')
		line.pop_back();
	return (true);
}

void Player::queue_output(const std::string &msg) {
	this->m_out_buffer += msg;
}

bool Player::push_command(const std::string &cmd) {
	if (this->m_commands.size() >= MAX_PENDING_COMMANDS)
		return (false);
	this->m_commands.push_back(cmd);
	return (true);
}
