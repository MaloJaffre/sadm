#!/usr/local/rvm/wrappers/redmine/ruby

require 'json'

conf = JSON.parse(STDIN.read)

users = conf['users']
commands = conf['commands']

commands.each do |login, cmd|
        user = users[login]

        case cmd
        when 'created'
                u = User.new(
                        :firstname => user['firstname'],
                        :lastname => user['lastname'],
                        :mail => login + '@finale.prologin'
                )
                u.login = login
                u.password = user['password']
                u.password_confirmation = user['password']
                u.admin = user['group'] == 'root'
                if u.valid?
                        u.save
                else
                        u = User.find_by_login(login)
                        if u then
                                u.password = user['password']
                                u.password_confirmation = user['password']
                                u.firstname = user['firstname']
                                u.lastname = user['lastname']
                                u.admin = user['group'] == 'root'
                                if u.valid? then
                                        $stderr.puts "got created user, actually was updated: #{login}"
                                        u.save
                                end
                        else
                                $stderr.puts "invalid created user: #{login}"
                                $stderr.puts "#{u.errors.full_messages}"
                        end
                end

        when 'deleted'
                u = User.find_by_login(login)
                u.destroy

        when 'updated'
                u = User.find_by_login(login)
                u.password = user['password']
                u.password_confirmation = user['password']
                u.firstname = user['firstname']
                u.lastname = user['lastname']
                u.admin = user['group'] == 'root'
                if u.valid? then
                        u.save
                else
                        $stderr.puts "invalid updated user: #{login}"
                        $stderr.puts "#{u.errors.full_messages}"
                end
        end

        if cmd == 'created' || cmd == 'updated'
                u = User.find_by_login(login)
                ['user', 'orga', 'root'].each do |gname|
                        g = Group.where(lastname: gname).first_or_create
                        g.save
                        g.users.delete(u)
                end

                g = Group.where(lastname: user['group']).first
                g.users << u
        end

end

__END__
